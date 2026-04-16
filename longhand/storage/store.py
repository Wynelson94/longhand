"""
LonghandStore — unified storage interface combining SQLite and ChromaDB.

SQLite is the source of truth. ChromaDB is the search index.
"""

from __future__ import annotations

import hashlib
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
from longhand.storage.vector_store import VectorStore
from longhand.types import Event, Session

DEFAULT_DATA_DIR = Path.home() / ".longhand"


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
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
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
        1. SQLite events + session
        2. Vector store embeddings
        3. Tool pair linking (call ↔ result)
        4. Ingestion log
        5. (optional) Analysis: project inference, outcome, episodes, session embedding
        """
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

        return result

    def analyze_session(self, session: Session, events: list[Event]) -> dict:
        """Run the analysis layer for a session: project, outcome, episodes, embeddings.

        Safe to call multiple times (upserts everywhere). Used both by `ingest_session`
        and by the `longhand analyze --all` backfill command.
        """
        # 1. Project inference + merge
        project = infer_project(session, events)
        self.sqlite.upsert_project(project)
        self.sqlite.attach_session_to_project(session.session_id, project["project_id"])

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

        # 3a. Episode embeddings — problem + diagnosis + fix text, semantic-searchable
        for ep in episodes:
            text = _build_episode_text(ep)
            if not text:
                continue
            self.vectors.add_episode_embedding(
                episode_id=ep["episode_id"],
                text=text,
                metadata={
                    "session_id": session.session_id,
                    "project_id": project["project_id"] or "",
                    "ended_at": ep["ended_at"],
                    "status": ep.get("status", "unresolved"),
                    "has_fix": bool(ep.get("fix_event_id")),
                },
            )

        # 3b. Conversation segment extraction
        segments = extract_segments(
            session_id=session.session_id,
            project_id=project["project_id"],
            events=events,
        )
        segments_stored = self.sqlite.insert_segments(segments)

        # 3c. Segment embeddings
        for seg in segments:
            self.vectors.add_segment_embedding(
                segment_id=seg["segment_id"],
                text=seg["summary"],
                metadata={
                    "session_id": session.session_id,
                    "project_id": project["project_id"] or "",
                    "segment_type": seg.get("segment_type", "discussion"),
                    "started_at": seg["started_at"],
                    "ended_at": seg["ended_at"],
                },
            )

        # 4. Session summary embedding
        session_text = build_session_text(session, events, outcome, project)
        session_meta = build_session_metadata(session, outcome, project)
        self.vectors.add_session_embedding(
            session_id=session.session_id,
            text=session_text,
            metadata=session_meta,
        )

        return {
            "project_id": project["project_id"],
            "outcome": outcome["outcome"],
            "episodes": episodes_stored,
            "segments": segments_stored,
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

            op_id = "gitop_" + hashlib.sha256(
                f"{session_id}:{e.event_id}".encode()
            ).hexdigest()[:16]
            ops.append({
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
            })
        return ops

    def stats(self) -> dict:
        sql_stats = self.sqlite.get_stats()
        sql_stats["vectors_indexed"] = self.vectors.count()
        return sql_stats

    def backfill_episode_embeddings(self, progress: Callable[[int, int], None] | None = None) -> int:
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

        embedded = 0
        for ep in episodes:
            text = _build_episode_text(ep)
            if not text:
                continue
            self.vectors.add_episode_embedding(
                episode_id=ep["episode_id"],
                text=text,
                metadata={
                    "session_id": ep.get("session_id") or "",
                    "project_id": ep.get("project_id") or "",
                    "ended_at": ep.get("ended_at") or "",
                    "status": ep.get("status", "unresolved"),
                    "has_fix": bool(ep.get("fix_event_id")),
                },
            )
            embedded += 1
            if progress and embedded % 25 == 0:
                progress(embedded, total)

        if progress:
            progress(embedded, total)
        return embedded

    def ensure_episode_embeddings(self) -> int:
        """If the episodes vector collection is empty but SQLite has episodes,
        transparently backfill. Returns the number of episodes embedded
        (0 if no backfill was needed). Safe to call on every recall.
        """
        try:
            vector_count = self.vectors.episode_count()
        except Exception:
            return 0
        if vector_count > 0:
            return 0
        sql_count = self.sqlite.count_episodes()
        if sql_count == 0:
            return 0
        return self.backfill_episode_embeddings()
