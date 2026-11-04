"""Reconcile session transcripts on disk against the sessions table.

Shared core for `longhand reconcile` (CLI) and the `reconcile` MCP tool.
Classifies every Claude Code JSONL into buckets and, when `fix=True`,
re-ingests the missing, null-project, partially-indexed, and changed entries.

"Changed" means the file grew after its last full ingest and has been quiet
for DEFAULT_FINALIZE_AFTER_SECONDS: a turn its session's own Stop and
SessionEnd hooks never captured (for example, a Stop hook that found the
ingest lock busy just before the session ended). A grown file written more
recently than that is "settling" and is left to those hooks.

Codex rollouts ride along when a Codex home exists: new or changed ones are
captured exact-record-only under the same ingest lock, and rollouts that have
gone quiet get the full pipeline (the Codex twin of Claude's SessionEnd hook),
so the scheduled reconciler keeps both clients' history current and recallable
with no extra setup.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from longhand.codex import DEFAULT_FINALIZE_AFTER_SECONDS
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
    changed: list[str] = field(default_factory=list)  # grew since last full ingest, now quiet
    settling_count: int = 0  # grew since last full ingest, still being written
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
    codex_unfinalized: int = 0  # quiet but not yet through the full pipeline
    codex_settling: int = 0  # written too recently to finalize this run
    codex_finalized: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "files_on_disk": self.files_on_disk,
            "fully_indexed": self.fully_indexed,
            "null_project_count": len(self.null_project),
            "missing_count": len(self.missing),
            "partially_indexed_count": len(self.partially_indexed),
            "changed_count": len(self.changed),
            "settling_count": self.settling_count,
            "skipped_oversize_count": len(self.skipped_oversize),
            "skipped_oversize": self.skipped_oversize,
            "null_project": self.null_project,
            "missing": self.missing,
            "partially_indexed": self.partially_indexed,
            "changed": self.changed,
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
            "codex_unfinalized": self.codex_unfinalized,
            "codex_settling": self.codex_settling,
            "codex_finalized": self.codex_finalized,
        }


IngestState = Literal["current", "changed", "settling"]


def full_ingest_state(
    store: LonghandStore, path: str | Path, st: os.stat_result, now: float
) -> IngestState:
    """Whether a transcript's last full ingest still covers the file on disk.

    - "current": the file is the size it had at its last full ingest.
    - "changed": the size differs and the file has been quiet for
      DEFAULT_FINALIZE_AFTER_SECONDS, so no hook of its own is coming to
      capture the rest.
    - "settling": the size differs but the file was written recently; its
      session's Stop and SessionEnd hooks still own it.

    Compares against `file_size`, which only a full ingest records. The Stop
    hook's live tail advances `last_offset` instead and never embeds or
    analyzes, so a file it has read to the end can still be "changed".
    Shared by reconcile and doctor's "Recent ingest" row.
    """
    if store.sqlite.already_ingested(str(path), st.st_size):
        return "current"
    if now - st.st_mtime >= DEFAULT_FINALIZE_AFTER_SECONDS:
        return "changed"
    return "settling"


def run_reconcile(
    store: LonghandStore, fix: bool = False, *, codex_home: str | Path | None = None
) -> ReconcileReport:
    """Classify on-disk JSONLs vs. indexed sessions; optionally re-ingest problem buckets.

    Without `fix`: returns counts only.
    With `fix=True`: acquires the ingest lock, re-ingests missing,
    null-project, partially-indexed, and changed Claude entries using current
    project inference, captures new or changed Codex rollouts (exact records
    only), and finalizes Codex rollouts that have been quiet for
    DEFAULT_FINALIZE_AFTER_SECONDS (full pipeline, so recall sees them).
    Claude transcripts that grew but were written within that window are
    only counted (`settling_count`). If another ingest is running, returns
    with `lock_unavailable=True` and nothing ingested.
    """
    from longhand.codex import (
        DEFAULT_SESSION_LIMIT,
        CodexArchiveStore,
        finalize_codex,
        scan_codex_sessions,
        sync_codex,
    )

    files = discover_sessions()
    now = time.time()

    missing: list[Path] = []
    null_project: list[Path] = []
    partial: list[Path] = []
    changed: list[Path] = []
    oversize: list[Path] = []
    fully_indexed = 0
    settling = 0
    stages: dict[str, str | None] = {}
    if files:
        with store.sqlite.connect() as conn:
            rows = conn.execute("SELECT transcript_path, project_id FROM sessions").fetchall()
        indexed: dict[str, str | None] = {r[0]: r[1] for r in rows}
        stages = store.sqlite.analysis_stages()

        for f in files:
            try:
                st: os.stat_result | None = f.stat()
            except OSError:
                st = None
            # Files past the parser's size cap can't be ingested by ANY path —
            # report them instead of erroring on every --fix pass (or worse,
            # sitting silently in the missing bucket forever).
            if st is not None and st.st_size > MAX_FILE_SIZE_BYTES:
                oversize.append(f)
                continue
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
            elif st is None:
                # Vanished between discovery and stat: keep the row-based answer.
                fully_indexed += 1
            else:
                ingest_state = full_ingest_state(store, f, st, now)
                if ingest_state == "changed":
                    changed.append(f)
                elif ingest_state == "settling":
                    settling += 1
                else:
                    fully_indexed += 1

    codex_scan = scan_codex_sessions(store.sqlite, codex_home)
    finalize_scan = scan_codex_sessions(
        store.sqlite,
        codex_home,
        complete_stages=("analyzed",),
        min_idle_seconds=DEFAULT_FINALIZE_AFTER_SECONDS,
    )

    report = ReconcileReport(
        files_on_disk=len(files),
        fully_indexed=fully_indexed,
        null_project=[str(p) for p in null_project],
        missing=[str(p) for p in missing],
        partially_indexed=[str(p) for p in partial],
        changed=[str(p) for p in changed],
        settling_count=settling,
        skipped_oversize=[str(p) for p in oversize],
        codex_on_disk=codex_scan.on_disk,
        codex_pending=len(codex_scan.candidates),
        codex_skipped_subagents=len(codex_scan.subagents),
        codex_oversize=len(codex_scan.oversize),
        codex_unfinalized=len(finalize_scan.candidates),
        codex_settling=len(finalize_scan.settling),
    )

    if not fix:
        return report

    # (path, run_analysis). A changed transcript whose last full ingest skipped
    # analysis on purpose (stage 'events', owned by `longhand analyze --all`)
    # gets its events healed without running that deferred analysis.
    to_process: list[tuple[Path, bool]] = [(f, True) for f in missing + null_project + partial]
    to_process += [(f, stages.get(str(f)) != "events") for f in changed]
    if not to_process and not codex_scan.candidates and not finalize_scan.candidates:
        report.fix_applied = True
        return report

    if not claim_ingest_lock(store):
        report.lock_unavailable = True
        return report

    try:
        for f, run_analysis in to_process:
            try:
                parser = JSONLParser(f)
                events = list(parser.parse_events())
                if not events:
                    continue
                session = parser.build_session(events)
                store.ingest_session(
                    session, events, run_analysis=run_analysis, transcript_size=parser.parsed_size
                )
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

        if finalize_scan.candidates:
            # The store already holds the model and the lock; as the catch-up
            # job, clear the whole quiet backlog rather than one per run.
            finalized = finalize_codex(
                store.sqlite,
                codex_home,
                lambda: store,
                limit=DEFAULT_SESSION_LIMIT,
                claim_lock=False,
            )
            report.codex_finalized = finalized["finalized"]
            report.codex_settling = finalized["settling"]
            report.errors.extend(finalized["errors"])
    finally:
        release_ingest_lock(store)

    report.fix_applied = True
    return report
