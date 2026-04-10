"""
LonghandStore — unified storage interface combining SQLite and ChromaDB.

SQLite is the source of truth. ChromaDB is the search index.
"""

from __future__ import annotations

from pathlib import Path

from longhand.analysis.episode_extraction import extract_episodes
from longhand.analysis.outcomes import classify_session
from longhand.analysis.project_inference import infer_project
from longhand.analysis.session_summary_embedding import (
    build_project_text,
    build_session_metadata,
    build_session_text,
)
from longhand.storage.sqlite_store import SQLiteStore
from longhand.storage.vector_store import VectorStore
from longhand.types import Event, Session


DEFAULT_DATA_DIR = Path.home() / ".longhand"


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
        }

    def stats(self) -> dict:
        sql_stats = self.sqlite.get_stats()
        sql_stats["vectors_indexed"] = self.vectors.count()
        return sql_stats
