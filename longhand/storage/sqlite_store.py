"""
SQLite storage for structured event data.

Every Event is persisted with its full raw JSON preserved as a blob.
Structured columns enable fast filtering by session, type, tool, file, and time.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from longhand.types import Event, EventType, FileOperation, Session


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project_path TEXT,
    transcript_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    event_count INTEGER DEFAULT 0,
    user_message_count INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    file_edit_count INTEGER DEFAULT 0,
    git_branch TEXT,
    cwd TEXT,
    model TEXT,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_event_id TEXT,
    event_type TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    content TEXT NOT NULL,
    is_sidechain INTEGER DEFAULT 0,
    tool_name TEXT,
    tool_use_id TEXT,
    tool_input_json TEXT,
    tool_output TEXT,
    tool_success INTEGER,
    file_path TEXT,
    file_operation TEXT,
    old_content TEXT,
    new_content TEXT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_tool ON events(tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_file ON events(file_path) WHERE file_path IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_tool_use_id ON events(tool_use_id) WHERE tool_use_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ingestion_log (
    transcript_path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    event_count INTEGER NOT NULL
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class SQLiteStore:
    """SQLite-backed structured storage for Longhand events and sessions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_session(self, session: Session) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, project_path, transcript_path, started_at, ended_at,
                    event_count, user_message_count, assistant_message_count,
                    tool_call_count, file_edit_count, git_branch, cwd, model, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    project_path = excluded.project_path,
                    transcript_path = excluded.transcript_path,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    event_count = excluded.event_count,
                    user_message_count = excluded.user_message_count,
                    assistant_message_count = excluded.assistant_message_count,
                    tool_call_count = excluded.tool_call_count,
                    file_edit_count = excluded.file_edit_count,
                    git_branch = excluded.git_branch,
                    cwd = excluded.cwd,
                    model = excluded.model,
                    ingested_at = excluded.ingested_at
                """,
                (
                    session.session_id,
                    session.project_path,
                    session.transcript_path,
                    _iso(session.started_at),
                    _iso(session.ended_at),
                    session.event_count,
                    session.user_message_count,
                    session.assistant_message_count,
                    session.tool_call_count,
                    session.file_edit_count,
                    session.git_branch,
                    session.cwd,
                    session.model,
                    _iso(datetime.now()),
                ),
            )

    def insert_events(self, events: list[Event]) -> int:
        if not events:
            return 0
        with self.connect() as conn:
            rows = [
                (
                    e.event_id,
                    e.session_id,
                    e.parent_event_id,
                    e.event_type if isinstance(e.event_type, str) else e.event_type.value,
                    e.sequence,
                    _iso(e.timestamp),
                    e.cwd,
                    e.git_branch,
                    e.model,
                    e.content,
                    int(e.is_sidechain),
                    e.tool_name,
                    e.tool_use_id,
                    json.dumps(e.tool_input) if e.tool_input is not None else None,
                    e.tool_output,
                    int(e.tool_success) if e.tool_success is not None else None,
                    e.file_path,
                    e.file_operation if isinstance(e.file_operation, str) or e.file_operation is None else e.file_operation.value,
                    e.old_content,
                    e.new_content,
                    json.dumps(e.raw),
                )
                for e in events
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO events (
                    event_id, session_id, parent_event_id, event_type, sequence,
                    timestamp, cwd, git_branch, model, content, is_sidechain,
                    tool_name, tool_use_id, tool_input_json, tool_output, tool_success,
                    file_path, file_operation, old_content, new_content, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def log_ingestion(self, transcript_path: str, session_id: str, file_size: int, event_count: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ingestion_log
                    (transcript_path, session_id, ingested_at, file_size, event_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (transcript_path, session_id, _iso(datetime.now()), file_size, event_count),
            )

    def already_ingested(self, transcript_path: str, current_size: int) -> bool:
        """Check if a file has been ingested at its current size (for skip on re-ingest)."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT file_size FROM ingestion_log WHERE transcript_path = ?",
                (transcript_path,),
            ).fetchone()
            if row is None:
                return False
            return row["file_size"] == current_size

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(
        self,
        project_path: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            query = "SELECT * FROM sessions"
            params: list[Any] = []
            if project_path:
                query += " WHERE project_path LIKE ?"
                params.append(f"%{project_path}%")
            query += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_events(
        self,
        session_id: str | None = None,
        event_type: EventType | str | None = None,
        tool_name: str | None = None,
        file_path: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            conditions: list[str] = []
            params: list[Any] = []
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if event_type:
                conditions.append("event_type = ?")
                params.append(event_type.value if isinstance(event_type, EventType) else event_type)
            if tool_name:
                conditions.append("tool_name = ?")
                params.append(tool_name)
            if file_path:
                conditions.append("file_path LIKE ?")
                params.append(f"%{file_path}%")

            query = "SELECT * FROM events"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp ASC, sequence ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
            return dict(row) if row else None

    def get_file_edits(self, file_path: str, session_id: str | None = None) -> list[dict[str, Any]]:
        """Get all edits to a specific file, in chronological order."""
        with self.connect() as conn:
            query = """
                SELECT * FROM events
                WHERE file_path = ?
                  AND event_type = 'tool_call'
                  AND file_operation IN ('edit', 'write', 'multi_edit', 'notebook_edit')
            """
            params: list[Any] = [file_path]
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            query += " ORDER BY timestamp ASC, sequence ASC"
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict[str, int]:
        with self.connect() as conn:
            stats = {
                "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "tool_calls": conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'tool_call'"
                ).fetchone()[0],
                "thinking_blocks": conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'assistant_thinking'"
                ).fetchone()[0],
                "file_edits": conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'tool_call' AND file_operation IN ('edit', 'write', 'multi_edit', 'notebook_edit')"
                ).fetchone()[0],
            }
            return stats
