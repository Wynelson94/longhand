"""
LonghandStore — unified storage interface combining SQLite and ChromaDB.

SQLite is the source of truth. ChromaDB is the search index.
"""

from __future__ import annotations

from pathlib import Path

from longhand.storage.sqlite_store import SQLiteStore
from longhand.storage.vector_store import VectorStore
from longhand.types import Event, Session


DEFAULT_DATA_DIR = Path.home() / ".longhand"


class LonghandStore:
    """Combined storage for Longhand: SQLite (truth) + ChromaDB (search)."""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.sqlite = SQLiteStore(self.data_dir / "longhand.db")
        self.vectors = VectorStore(self.data_dir / "chroma")

    def ingest_session(self, session: Session, events: list[Event]) -> dict[str, int]:
        """Persist a parsed session and its events to both backends."""
        self.sqlite.upsert_session(session)
        sql_inserted = self.sqlite.insert_events(events)
        vec_inserted = self.vectors.add_events(events)

        transcript_size = Path(session.transcript_path).stat().st_size if Path(session.transcript_path).exists() else 0
        self.sqlite.log_ingestion(session.transcript_path, session.session_id, transcript_size, len(events))

        return {
            "events_stored": sql_inserted,
            "events_indexed": vec_inserted,
        }

    def stats(self) -> dict:
        sql_stats = self.sqlite.get_stats()
        sql_stats["vectors_indexed"] = self.vectors.count()
        return sql_stats
