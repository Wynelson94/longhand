"""
LonghandStore — unified storage interface combining SQLite and ChromaDB.

SQLite is the source of truth. ChromaDB is the search index.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path

from longhand.analysis.episode_extraction import extract_episodes
from longhand.analysis.outcomes import classify_session
from longhand.analysis.project_inference import infer_project
from longhand.analysis.segment_extraction import extract_segments
from longhand.analysis.session_summary_embedding import (
    build_project_text,
    build_session_metadata,
    build_session_text,
)
from longhand.storage.sqlite_store import SQLiteStore
from longhand.storage.vector_store import CHROMA_BATCH_SIZE, VectorStore
from longhand.types import Event, Session

DEFAULT_DATA_DIR = Path.home() / ".longhand"


def resolve_data_dir(flag_value: str | Path | None = None) -> Path:
    """Longhand's data directory — the single resolution rule.

    Precedence: explicit flag/argument > LONGHAND_DATA_DIR env var >
    ~/.longhand. The env var is what carries a relocated store into hooks,
    spawned workers, and the MCP server: child processes inherit it, so
    nothing else needs a flag.
    """
    if flag_value:
        return Path(flag_value)
    env = os.environ.get("LONGHAND_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return DEFAULT_DATA_DIR


def data_dir_source(flag_value: str | Path | None = None) -> str:
    """Which precedence level won — doctor prints this next to the path."""
    if flag_value:
        return "--data-dir flag"
    if os.environ.get("LONGHAND_DATA_DIR", "").strip():
        return "LONGHAND_DATA_DIR"
    return "default"


def _build_episode_text(episode: dict) -> str:
    """Compose the embeddable text for a problem→fix episode.

    Joins the episode's three narrative fields (problem / diagnosis / fix)
    with labeled sentinels so the embedding carries structural cues and
    degrades gracefully when one of the fields is empty.
    """
    parts: list[str] = []
    problem = (episode.get("problem_description") or "").strip()
    diagnosis = (episode.get("diagnosis_summary") or "").strip()
    fix = (episode.get("fix_summary") or "").strip()
    if problem:
        parts.append(f"Problem: {problem}")
    if diagnosis:
        parts.append(f"Diagnosis: {diagnosis}")
    if fix:
        parts.append(f"Fix: {fix}")
    return "\n".join(parts)


class LonghandStore:
    """Combined storage for Longhand: SQLite (truth) + ChromaDB (search)."""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = resolve_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        self.sqlite = SQLiteStore(self.data_dir / "longhand.db")
        self.vectors = VectorStore(self.data_dir / "chroma")

    def ingest_session(
        self,
        session: Session,
        events: list[Event],
        run_analysis: bool = True,
    ) -> dict:
        """Persist a parsed session and its events to both backends.

        Pipeline:
        0. Mark intent (analysis_stage='pending' — a crash below leaves this
           marker for reconcile's partially_indexed bucket)
        1. SQLite events + session
        2. Vector store embeddings
        3. Tool pair linking (call ↔ result)
        4. Ingestion log
        5. (optional) Analysis: project inference, outcome, episodes, session
           embedding — stamps analysis_stage='analyzed' on completion;
           run_analysis=False stamps 'events' (deliberate defer, not a crash)

        Each step commits separately; the stage marker is what makes a
        mid-pipeline failure visible instead of silently under-analyzed.
        """
        self.sqlite.mark_ingest_started(session.transcript_path, session.session_id)

        self.sqlite.upsert_session(session)
        sql_inserted = self.sqlite.insert_events(events)
        vec_inserted = self.vectors.add_events(events)

        pairs = self.sqlite.build_tool_pairs_from_events(events)
        pairs_stored = self.sqlite.upsert_tool_pairs(pairs)

        # Extract git operations from Bash tool results
        git_ops = self._extract_git_operations(session.session_id, events)
        git_ops_stored = self.sqlite.insert_git_operations(git_ops)

        transcript_size = (
            Path(session.transcript_path).stat().st_size
            if Path(session.transcript_path).exists()
            else 0
        )
        self.sqlite.log_ingestion(
            session.transcript_path, session.session_id, transcript_size, len(events)
        )

        result: dict = {
            "events_stored": sql_inserted,
            "events_indexed": vec_inserted,
            "tool_pairs": pairs_stored,
            "git_operations": git_ops_stored,
            "episodes": 0,
        }

        if run_analysis:
            analysis_result = self.analyze_session(session, events)
            result.update(analysis_result)
        else:
            self.sqlite.set_analysis_stage(session.transcript_path, "events")

        return result

    def attribute_session_project(self, session: Session, events: list[Event]) -> dict:
        """Infer this session's project, attach it, and refresh the project's
        derived rollups — WITHOUT the heavy episode/segment/embedding analysis.

        This is the lockstep primitive: project_id is always derived from the same
        (session, events) pair that determines the session's cwd, so the two can't
        desync. Used by analyze_session (full pass), the live-tail hook, and the
        reattribute backfill. session_count / total_edits are recomputed from the
        sessions table (not incremented), so repeated calls never inflate them.
        """
        project = infer_project(session, events)
        self.sqlite.upsert_project(project)
        self.sqlite.attach_session_to_project(session.session_id, project["project_id"])
        self.sqlite.recompute_project_stats(project["project_id"])
        return project

    def reattribute_sessions(
        self, session_ids: list[str] | None = None, apply: bool = True
    ) -> dict:
        """Re-derive each session's project (and corrected cwd) from its events in
        the events table — independent of the transcript file, which may have been
        rotated off disk — and re-attach it.

        Repairs project_id↔cwd drift (the F2 misattribution bug, where sessions
        ended up filed under the home catch-all). With ``apply=False`` nothing
        is written — the returned ``changes`` list shows what would move.

        Returns ``{scanned, reattributed, skipped, applied, changes, pruned}``;
        ``changes`` is ``[{session_id, old_project_id, new_project_id,
        new_display_name}]``, ``pruned`` counts project rows deleted because
        the moves left them with zero sessions (dry-run: would-be count).
        """
        from longhand.analysis.project_inference import infer_project
        from longhand.parser import _pick_best_project_cwd

        rows = self.sqlite.list_sessions(limit=1_000_000)
        if session_ids is not None:
            wanted = set(session_ids)
            rows = [r for r in rows if r["session_id"] in wanted]

        scanned = skipped = 0
        changes: list[dict] = []
        for row in rows:
            sid = row["session_id"]
            events = self._events_for_session(sid)
            if not events:
                skipped += 1
                continue
            best_cwd = _pick_best_project_cwd(events) or row.get("cwd") or row.get("project_path")
            session = self._session_from_row(row, best_cwd)
            old_pid = row.get("project_id")

            if apply:
                project = self.attribute_session_project(session, events)
                if best_cwd and best_cwd != row.get("cwd"):
                    with self.sqlite.connect() as conn:
                        conn.execute(
                            "UPDATE sessions SET cwd = ? WHERE session_id = ?", (best_cwd, sid)
                        )
            else:
                project = infer_project(session, events)

            scanned += 1
            if project["project_id"] != old_pid:
                changes.append(
                    {
                        "session_id": sid,
                        "old_project_id": old_pid,
                        "new_project_id": project["project_id"],
                        "new_display_name": project["display_name"],
                    }
                )

        pruned = 0
        if apply:
            # Sessions may have moved between projects — re-derive every project's
            # rollups so counts stay accurate, then drop rows the moves emptied
            # (completes case-dupe/junk-project merges).
            with self.sqlite.connect() as conn:
                pids = [r[0] for r in conn.execute("SELECT project_id FROM projects").fetchall()]
            for pid in pids:
                self.sqlite.recompute_project_stats(pid)
            pruned = self.sqlite.prune_empty_projects()
        elif changes:
            moved_from = {c["old_project_id"] for c in changes if c["old_project_id"]}
            with self.sqlite.connect() as conn:
                for pid in moved_from:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM sessions WHERE project_id = ?", (pid,)
                    ).fetchone()[0]
                    moving = sum(1 for c in changes if c["old_project_id"] == pid)
                    if remaining - moving <= 0:
                        pruned += 1

        return {
            "scanned": scanned,
            "reattributed": len(changes),
            "skipped": skipped,
            "applied": apply,
            "changes": changes,
            "pruned": pruned,
        }

    def _events_for_session(self, session_id: str) -> list[Event]:
        """Rebuild Event objects from the events table. Carries every field
        the analysis layer reads (attribution, outcomes, episode extraction),
        so rotated-off-disk transcripts can still be fully re-analyzed.
        (error_category isn't stored as a column — episode tags degrade
        gracefully without it.)"""
        from longhand.parser import _parse_timestamp

        rows = self.sqlite.get_events(session_id=session_id, limit=1_000_000)
        events: list[Event] = []
        for r in rows:
            try:
                success = r.get("tool_success")
                events.append(
                    Event(
                        event_id=r["event_id"],
                        session_id=r["session_id"],
                        parent_event_id=r.get("parent_event_id"),
                        event_type=r["event_type"],
                        sequence=r["sequence"],
                        timestamp=_parse_timestamp(r.get("timestamp")),
                        cwd=r.get("cwd"),
                        git_branch=r.get("git_branch"),
                        model=r.get("model"),
                        content=r.get("content") or "",
                        is_sidechain=bool(r.get("is_sidechain")),
                        tool_name=r.get("tool_name"),
                        tool_use_id=r.get("tool_use_id"),
                        tool_output=r.get("tool_output"),
                        tool_success=None if success is None else bool(success),
                        file_path=r.get("file_path"),
                        file_operation=r.get("file_operation"),
                        old_content=r.get("old_content"),
                        new_content=r.get("new_content"),
                        error_detected=bool(r.get("error_detected")),
                        error_snippet=r.get("error_snippet"),
                    )
                )
            except Exception:
                continue
        return events

    def load_session_from_db(self, row: dict) -> tuple[Session, list[Event]] | None:
        """Rebuild (session, events) from the events table — for sessions whose
        transcript has rotated off disk. Same source `reattribute` uses, so
        `analyze --all` can re-extract the whole corpus, not just the ~25% of
        sessions whose JSONLs still exist."""
        from longhand.parser import _pick_best_project_cwd

        events = self._events_for_session(row["session_id"])
        if not events:
            return None
        best_cwd = _pick_best_project_cwd(events) or row.get("cwd") or row.get("project_path")
        return self._session_from_row(row, best_cwd), events

    def _session_from_row(self, row: dict, cwd: str | None) -> Session:
        from longhand.parser import _parse_timestamp

        return Session(
            session_id=row["session_id"],
            project_path=cwd or row.get("project_path"),
            transcript_path=row.get("transcript_path") or "",
            started_at=_parse_timestamp(row.get("started_at")),
            ended_at=_parse_timestamp(row.get("ended_at")),
            event_count=row.get("event_count") or 0,
            file_edit_count=row.get("file_edit_count") or 0,
            git_branch=row.get("git_branch"),
            cwd=cwd,
            model=row.get("model"),
        )

    def analyze_session(self, session: Session, events: list[Event]) -> dict:
        """Run the analysis layer for a session: project, outcome, episodes, embeddings.

        Safe to call multiple times — re-analysis REPLACES the session's
        episodes and segments. Episode/segment ids hash the start position, so
        when a new extractor draws different boundaries the fresh rows get new
        ids; without the delete below, the old-boundary rows would linger as
        stale duplicates forever. Used both by `ingest_session` and by the
        `longhand analyze --all` backfill command.
        """
        self.sqlite.delete_session_analysis(session.session_id)
        self.vectors.delete_session_analysis(session.session_id)

        # 1. Project inference + attach (cwd↔project_id kept in lockstep here).
        project = self.attribute_session_project(session, events)

        # 1b. Project embedding
        self.vectors.add_project_embedding(
            project_id=project["project_id"],
            text=build_project_text(project),
            metadata={
                "display_name": project["display_name"],
                "category": project.get("category") or "",
            },
        )

        # 2. Outcome classification
        outcome = classify_session(session, events)
        self.sqlite.upsert_outcome(outcome)

        # 3. Episode extraction
        episodes = extract_episodes(
            session_id=session.session_id,
            project_id=project["project_id"],
            events=events,
        )
        episodes_stored = self.sqlite.insert_episodes(episodes)

        # 3a. Episode embeddings — only episodes with an identified fix.
        # Fixless episodes (e.g., "command not found" that never led to a
        # file edit) stay in SQLite for forensic access via find_episodes,
        # patterns, recap, and export — but they clutter the vector space
        # with thin snippets of raw error text, so we skip embedding them.
        # Collect first, then batch-upsert so ONNX can embed in one pass.
        episode_items: list[dict] = []
        for ep in episodes:
            if not ep.get("fix_event_id"):
                continue
            text = _build_episode_text(ep)
            if not text:
                continue
            episode_items.append(
                {
                    "episode_id": ep["episode_id"],
                    "text": text,
                    "metadata": {
                        "session_id": session.session_id,
                        "project_id": project["project_id"] or "",
                        "ended_at": ep["ended_at"],
                        "status": ep.get("status", "unresolved"),
                        "has_fix": True,
                    },
                }
            )
        episodes_embedded = self.vectors.add_episode_embeddings_batch(episode_items)

        # 3b. Conversation segment extraction
        segments = extract_segments(
            session_id=session.session_id,
            project_id=project["project_id"],
            events=events,
        )
        segments_stored = self.sqlite.insert_segments(segments)

        # 3c. Segment embeddings — batched for the same reason as 3a.
        segment_items = [
            {
                "segment_id": seg["segment_id"],
                "text": seg["summary"],
                "metadata": {
                    "session_id": session.session_id,
                    "project_id": project["project_id"] or "",
                    "segment_type": seg.get("segment_type", "discussion"),
                    "started_at": seg["started_at"],
                    "ended_at": seg["ended_at"],
                },
            }
            for seg in segments
        ]
        segments_embedded = self.vectors.add_segment_embeddings_batch(segment_items)

        # 4. Session summary embedding
        session_text = build_session_text(session, events, outcome, project)
        session_meta = build_session_metadata(session, outcome, project)
        self.vectors.add_session_embedding(
            session_id=session.session_id,
            text=session_text,
            metadata=session_meta,
        )

        # Full pipeline done for this transcript — clears the 'pending' crash
        # marker. Also covers `longhand analyze --all`, which calls this
        # directly. No-op when the transcript has no ingestion_log row.
        self.sqlite.set_analysis_stage(session.transcript_path, "analyzed")

        return {
            "project_id": project["project_id"],
            "outcome": outcome["outcome"],
            "episodes": episodes_stored,
            "episodes_embedded": episodes_embedded,
            "segments": segments_stored,
            "segments_embedded": segments_embedded,
        }

    @staticmethod
    def _extract_git_operations(session_id: str, events: list[Event]) -> list[dict]:
        """Build git_operations rows from events with git_operation set.

        The full GitSignal is re-extracted here to capture fields (remote,
        files_changed_count, success) that aren't stored on the Event model.
        """
        from longhand.extractors.git import extract_git_signal

        ops: list[dict] = []
        # Build tool_input lookup for paired tool_calls
        tool_inputs: dict[str, dict] = {}
        for e in events:
            etype = e.event_type if isinstance(e.event_type, str) else e.event_type.value
            if etype == "tool_call" and e.tool_use_id and e.tool_input:
                tool_inputs[e.tool_use_id] = e.tool_input

        for e in events:
            if not e.git_operation:
                continue

            # Re-extract full signal to get remote, files_changed, success
            command = ""
            if e.tool_use_id:
                paired_input = tool_inputs.get(e.tool_use_id, {})
                command = paired_input.get("command", "")
            signal = extract_git_signal(command, e.tool_output or "")

            op_id = (
                "gitop_" + hashlib.sha256(f"{session_id}:{e.event_id}".encode()).hexdigest()[:16]
            )
            ops.append(
                {
                    "git_op_id": op_id,
                    "session_id": session_id,
                    "event_id": e.event_id,
                    "operation_type": e.git_operation,
                    "commit_hash": e.git_commit_hash,
                    "commit_message": e.git_commit_message,
                    "branch": signal.branch if signal else e.git_branch,
                    "remote": signal.remote if signal else None,
                    "files_changed_count": signal.files_changed_count if signal else None,
                    "timestamp": e.timestamp.isoformat(),
                    "success": signal.success if signal else True,
                }
            )
        return ops

    def stats(self) -> dict:
        sql_stats = self.sqlite.get_stats()
        sql_stats["vectors_indexed"] = self.vectors.count()
        return sql_stats

    def backfill_episode_embeddings(
        self, progress: Callable[[int, int], None] | None = None
    ) -> int:
        """Embed every episode row from SQLite into the vector store.

        Idempotent — upserts by episode_id. Needed once after upgrading from
        a pre-episodes-collection Longhand version; auto-called from the
        recall pipeline when the collection is empty but the SQLite table
        is populated.

        `progress` is an optional callback receiving (done, total) after
        each batch, for CLI progress bars.
        """
        episodes = self.sqlite.query_episodes(limit=100_000)
        total = len(episodes)
        if total == 0:
            return 0

        # Skip fixless episodes — they stay in SQLite but aren't embedded.
        # Keeps vector recall focused on episodes where there's an actual
        # fix to retrieve.
        items: list[dict] = []
        for ep in episodes:
            if not ep.get("fix_event_id"):
                continue
            text = _build_episode_text(ep)
            if not text:
                continue
            items.append(
                {
                    "episode_id": ep["episode_id"],
                    "text": text,
                    "metadata": {
                        "session_id": ep.get("session_id") or "",
                        "project_id": ep.get("project_id") or "",
                        "ended_at": ep.get("ended_at") or "",
                        "status": ep.get("status", "unresolved"),
                        "has_fix": True,
                    },
                }
            )

        if not items:
            if progress:
                progress(0, total)
            return 0

        embedded = 0
        # ONNX embeds far more efficiently when given items in one call,
        # but callers want incremental progress. Report at the same chunk
        # size Chroma upserts at, so each progress tick corresponds to one
        # real flush of embeddings.
        for i in range(0, len(items), CHROMA_BATCH_SIZE):
            embedded += self.vectors.add_episode_embeddings_batch(items[i : i + CHROMA_BATCH_SIZE])
            if progress:
                progress(embedded, total)

        return embedded

    def episode_backfill_needed(self) -> bool:
        """True when SQLite has episodes but the vector collection is empty —
        i.e. a first run after upgrading to a version with episode embeddings.
        Cheap (two counts); never raises.
        """
        try:
            if self.vectors.episode_count() > 0:
                return False
            return self.sqlite.count_episodes() > 0
        except Exception:
            return False

    def ensure_episode_embeddings(self) -> int:
        """If the episodes vector collection is empty but SQLite has episodes,
        transparently backfill. Returns the number of episodes embedded
        (0 if no backfill was needed).

        NB: this embeds inline — up to the whole corpus. The recall pipeline
        deliberately does NOT call it (it spawns a detached
        `longhand backfill-episodes` instead); this stays for the CLI and for
        callers that want the synchronous behavior.
        """
        if not self.episode_backfill_needed():
            return 0
        return self.backfill_episode_embeddings()
