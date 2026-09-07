"""Reconcile session transcripts on disk against the sessions table.

Shared core for `longhand reconcile` (CLI) and the `reconcile` MCP tool.
Classifies every Claude Code JSONL into buckets and, when `fix=True`,
re-ingests the missing, null-project, and partially-indexed entries.

Codex rollouts ride along when a Codex home exists: new or changed ones are
captured exact-record-only under the same ingest lock, so the scheduled
reconciler keeps both clients' history current with no extra setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from longhand.parser import MAX_FILE_SIZE_BYTES, JSONLParser, discover_sessions
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
    skipped_oversize: list[str] = field(default_factory=list)  # > parser size cap
    ingested: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)  # [{path, error}]
    fix_applied: bool = False
    lock_unavailable: bool = False
    # Codex rollouts — all zero on a machine without a Codex home.
    codex_on_disk: int = 0
    codex_pending: int = 0  # new or changed since capture, within the size bound
    codex_skipped_subagents: int = 0
    codex_oversize: int = 0
    codex_ingested: int = 0
    codex_deferred: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "files_on_disk": self.files_on_disk,
            "fully_indexed": self.fully_indexed,
            "null_project_count": len(self.null_project),
            "missing_count": len(self.missing),
            "partially_indexed_count": len(self.partially_indexed),
            "skipped_oversize_count": len(self.skipped_oversize),
            "skipped_oversize": self.skipped_oversize,
            "null_project": self.null_project,
            "missing": self.missing,
            "partially_indexed": self.partially_indexed,
            "ingested": self.ingested,
            "errors": self.errors,
            "fix_applied": self.fix_applied,
            "lock_unavailable": self.lock_unavailable,
            "codex_on_disk": self.codex_on_disk,
            "codex_pending": self.codex_pending,
            "codex_skipped_subagents": self.codex_skipped_subagents,
            "codex_oversize": self.codex_oversize,
            "codex_ingested": self.codex_ingested,
            "codex_deferred": self.codex_deferred,
        }


def run_reconcile(
    store: LonghandStore, fix: bool = False, *, codex_home: str | Path | None = None
) -> ReconcileReport:
    """Classify on-disk JSONLs vs. indexed sessions; optionally re-ingest problem buckets.

    Without `fix`: returns counts only.
    With `fix=True`: acquires the ingest lock, re-ingests missing,
    null-project, and partially-indexed Claude entries using current project
    inference, and captures new or changed Codex rollouts (exact records
    only). If another ingest is running, returns with
    `lock_unavailable=True` and nothing ingested.
    """
    from longhand.codex import CodexArchiveStore, scan_codex_sessions, sync_codex

    files = discover_sessions()

    missing: list[Path] = []
    null_project: list[Path] = []
    partial: list[Path] = []
    oversize: list[Path] = []
    fully_indexed = 0
    if files:
        with store.sqlite.connect() as conn:
            rows = conn.execute("SELECT transcript_path, project_id FROM sessions").fetchall()
        indexed: dict[str, str | None] = {r[0]: r[1] for r in rows}
        stages = store.sqlite.analysis_stages()

        for f in files:
            # Files past the parser's size cap can't be ingested by ANY path —
            # report them instead of erroring on every --fix pass (or worse,
            # sitting silently in the missing bucket forever).
            try:
                if f.stat().st_size > MAX_FILE_SIZE_BYTES:
                    oversize.append(f)
                    continue
            except OSError:
                pass
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

    codex_scan = scan_codex_sessions(store.sqlite, codex_home)

    report = ReconcileReport(
        files_on_disk=len(files),
        fully_indexed=fully_indexed,
        null_project=[str(p) for p in null_project],
        missing=[str(p) for p in missing],
        partially_indexed=[str(p) for p in partial],
        skipped_oversize=[str(p) for p in oversize],
        codex_on_disk=codex_scan.on_disk,
        codex_pending=len(codex_scan.candidates),
        codex_skipped_subagents=len(codex_scan.subagents),
        codex_oversize=len(codex_scan.oversize),
    )

    if not fix:
        return report

    to_process = missing + null_project + partial
    if not to_process and not codex_scan.candidates:
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

        if codex_scan.candidates:
            # Exact-record capture through the store's own SQLite handle; the
            # lock is already ours, so sync must not claim (or release) it.
            codex_report = sync_codex(
                CodexArchiveStore(store.data_dir, sqlite=store.sqlite),
                codex_home,
                claim_lock=False,
            )
            report.codex_ingested = codex_report["ingested"]
            report.codex_deferred = list(codex_report["deferred"])
            report.errors.extend(codex_report["errors"])
    finally:
        release_ingest_lock(store)

    report.fix_applied = True
    return report
