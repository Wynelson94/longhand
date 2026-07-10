"""Reconcile session transcripts on disk against the sessions table.

Shared core for `longhand reconcile` (CLI) and the `reconcile` MCP tool.
Classifies every on-disk JSONL into one of three buckets and, when
`fix=True`, re-ingests the missing and null-project-id entries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from longhand.parser import JSONLParser, discover_sessions
from longhand.recall.project_fallback import (
    claim_ingest_lock,
    release_ingest_lock,
)
from longhand.storage import LonghandStore


@dataclass
class ReconcileReport:
    files_on_disk: int
    fully_indexed: int
    null_project: list[str] = field(default_factory=list)  # transcript paths
    missing: list[str] = field(default_factory=list)
    partially_indexed: list[str] = field(default_factory=list)  # crashed mid-pipeline
    ingested: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)  # [{path, error}]
    fix_applied: bool = False
    lock_unavailable: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "files_on_disk": self.files_on_disk,
            "fully_indexed": self.fully_indexed,
            "null_project_count": len(self.null_project),
            "missing_count": len(self.missing),
            "partially_indexed_count": len(self.partially_indexed),
            "null_project": self.null_project,
            "missing": self.missing,
            "partially_indexed": self.partially_indexed,
            "ingested": self.ingested,
            "errors": self.errors,
            "fix_applied": self.fix_applied,
            "lock_unavailable": self.lock_unavailable,
        }


def run_reconcile(store: LonghandStore, fix: bool = False) -> ReconcileReport:
    """Classify on-disk JSONLs vs. indexed sessions; optionally re-ingest problem buckets.

    Without `fix`: returns counts only.
    With `fix=True`: acquires the ingest lock and re-ingests missing,
    null-project, and partially-indexed entries using current project
    inference. If another ingest is running, returns with
    `lock_unavailable=True` and zero ingested.
    """
    files = discover_sessions()
    if not files:
        return ReconcileReport(files_on_disk=0, fully_indexed=0)

    with store.sqlite.connect() as conn:
        rows = conn.execute("SELECT transcript_path, project_id FROM sessions").fetchall()
    indexed: dict[str, str | None] = {r[0]: r[1] for r in rows}
    stages = store.sqlite.analysis_stages()

    missing: list[Path] = []
    null_project: list[Path] = []
    partial: list[Path] = []
    fully_indexed = 0
    for f in files:
        state = indexed.get(str(f), "__not_found__")
        if state == "__not_found__":
            missing.append(f)
        elif state is None:
            null_project.append(f)
        elif stages.get(str(f)) == "pending":
            # The ingest pipeline started but never finished — a crash left
            # this session with events but possibly no analysis/vectors.
            # Only 'pending' counts: NULL is a pre-v0.12 or live-tail row
            # (unknown — treated as complete so upgrades don't stampede),
            # and 'events' is a deliberate --skip-analysis defer owned by
            # `longhand analyze --all`.
            partial.append(f)
        else:
            fully_indexed += 1

    report = ReconcileReport(
        files_on_disk=len(files),
        fully_indexed=fully_indexed,
        null_project=[str(p) for p in null_project],
        missing=[str(p) for p in missing],
        partially_indexed=[str(p) for p in partial],
    )

    if not fix:
        return report

    to_process = missing + null_project + partial
    if not to_process:
        report.fix_applied = True
        return report

    if not claim_ingest_lock(store):
        report.lock_unavailable = True
        return report

    try:
        for f in to_process:
            try:
                parser = JSONLParser(f)
                events = list(parser.parse_events())
                if not events:
                    continue
                session = parser.build_session(events)
                store.ingest_session(session, events, run_analysis=True)
                report.ingested += 1
            except Exception as e:  # noqa: BLE001
                report.errors.append({"path": str(f), "error": str(e)})
    finally:
        release_ingest_lock(store)

    report.fix_applied = True
    return report
