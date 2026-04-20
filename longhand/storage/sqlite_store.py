"""
SQLite storage for structured event data.

Every Event is persisted with its full raw JSON preserved as a blob.
Structured columns enable fast filtering by session, type, tool, file, and time.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from longhand.storage.migrations import apply_migrations
from longhand.types import Event, EventType, Session

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
    error_detected INTEGER DEFAULT 0,
    error_snippet TEXT,
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


# Maximum length we accept for any user-provided keyword/path filter.
# Caps DoS via giant LIKE patterns and absurd query payloads.
MAX_FILTER_LENGTH = 500


def _escape_like(value: str) -> str:
    r"""Escape SQLite LIKE wildcards (%, _, \) so user input matches literally.

    Caps the length to MAX_FILTER_LENGTH to prevent pathological pattern DoS.
    Use with `LIKE ? ESCAPE '\'` so SQLite knows to honor the backslash.
    """
    if not value:
        return ""
    truncated = value[:MAX_FILTER_LENGTH]
    return (
        truncated
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


class SQLiteStore:
    """SQLite-backed structured storage for Longhand events and sessions."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            apply_migrations(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
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
                    int(e.error_detected),
                    e.error_snippet,
                )
                for e in events
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO events (
                    event_id, session_id, parent_event_id, event_type, sequence,
                    timestamp, cwd, git_branch, model, content, is_sidechain,
                    tool_name, tool_use_id, tool_input_json, tool_output, tool_success,
                    file_path, file_operation, old_content, new_content, raw_json,
                    error_detected, error_snippet
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def build_tool_pairs_from_events(self, events: list[Event]) -> list[dict[str, Any]]:
        """Construct tool_pair rows from a list of parsed events.

        Links tool_use_id on TOOL_CALL events to matching tool_use_id on TOOL_RESULT events.
        """
        calls: dict[str, Event] = {}
        results: dict[str, Event] = {}
        for e in events:
            etype = e.event_type if isinstance(e.event_type, str) else e.event_type.value
            if etype == "tool_call" and e.tool_use_id:
                calls[e.tool_use_id] = e
            elif etype == "tool_result" and e.tool_use_id:
                results[e.tool_use_id] = e

        pairs: list[dict[str, Any]] = []
        for tool_use_id, call in calls.items():
            result = results.get(tool_use_id)
            pairs.append({
                "tool_use_id": tool_use_id,
                "call_event_id": call.event_id,
                "result_event_id": result.event_id if result else None,
                "success": result.tool_success if result else None,
                "error_detected": bool(result.error_detected) if result else False,
                "error_snippet": result.error_snippet if result else None,
            })
        return pairs

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
        project_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List sessions with optional filters.

        - project_path: substring match on the cwd path
        - project_id: exact match on the inferred project
        - since / until: ISO timestamps filtering by started_at
        - outcome: filter via session_outcomes join (e.g. 'fixed', 'stuck')
        """
        with self.connect() as conn:
            conditions: list[str] = []
            params: list[Any] = []

            if project_path:
                conditions.append("s.project_path LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(project_path)}%")
            if project_id:
                conditions.append("s.project_id = ?")
                params.append(project_id)
            if since:
                conditions.append("s.started_at >= ?")
                params.append(since)
            if until:
                conditions.append("s.started_at <= ?")
                params.append(until)

            base = "SELECT s.* FROM sessions s"
            if outcome:
                base += " INNER JOIN session_outcomes o ON o.session_id = s.session_id"
                conditions.append("o.outcome = ?")
                params.append(outcome)

            query = base
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY s.started_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_events(
        self,
        session_id: str | None = None,
        event_type: EventType | str | None = None,
        tool_name: str | None = None,
        file_path: str | None = None,
        since: str | None = None,
        until: str | None = None,
        has_error: bool | None = None,
        limit: int = 500,
        offset: int = 0,
        dedup_suffixes: bool = True,
    ) -> list[dict[str, Any]]:
        """Return events with optional filters. When `dedup_suffixes` is
        True (default), collision-resolution duplicates produced by the
        parser (event_id containing `#`) are hidden — users only see the
        primary row per logical event.
        """
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
                conditions.append("file_path LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(file_path)}%")
            if since:
                conditions.append("timestamp >= ?")
                params.append(since)
            if until:
                conditions.append("timestamp <= ?")
                params.append(until)
            if has_error is not None:
                conditions.append("COALESCE(error_detected, 0) = ?")
                params.append(1 if has_error else 0)
            if dedup_suffixes:
                conditions.append("event_id NOT LIKE '%#%'")

            query = "SELECT * FROM events"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp ASC, sequence ASC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
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

    def get_events_by_sequence_range(
        self,
        session_id: str,
        seq_start: int,
        seq_end: int,
        dedup_suffixes: bool = True,
    ) -> list[dict[str, Any]]:
        """Get events within a sequence number range for a session.

        `dedup_suffixes=True` (default) hides parser collision duplicates
        (event_id containing `#`) — otherwise a context window fetched
        with `seq_start=0, seq_end=5` returns 12+ events instead of 6
        when streaming-chunk collisions exist.
        """
        with self.connect() as conn:
            query = (
                "SELECT * FROM events WHERE session_id = ? AND sequence BETWEEN ? AND ? "
            )
            if dedup_suffixes:
                query += "AND event_id NOT LIKE '%#%' "
            query += "ORDER BY sequence ASC"
            rows = conn.execute(
                query,
                (session_id, seq_start, seq_end),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_latest_events(
        self,
        session_id: str,
        limit: int = 10,
        event_type: EventType | str | None = None,
        dedup_suffixes: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the N most recent events in a session, ordered by sequence DESC.

        Distinct from semantic `search` (which ranks by similarity) and
        `get_session_timeline` (which returns events in ascending order).
        Use this for "what was the latest X" questions.

        `dedup_suffixes=True` (default) hides parser collision duplicates
        (event_id containing `#`).
        """
        query = "SELECT * FROM events WHERE session_id = ?"
        params: list[Any] = [session_id]
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type.value if isinstance(event_type, EventType) else event_type)
        if dedup_suffixes:
            query += " AND event_id NOT LIKE '%#%'"
        query += " ORDER BY sequence DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
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
            # New proactive memory stats
            try:
                stats["projects"] = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
                stats["episodes"] = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                stats["resolved_episodes"] = conn.execute(
                    "SELECT COUNT(*) FROM episodes WHERE status = 'resolved'"
                ).fetchone()[0]
                stats["outcomes"] = conn.execute("SELECT COUNT(*) FROM session_outcomes").fetchone()[0]
            except sqlite3.OperationalError:
                pass
            try:
                stats["git_operations"] = conn.execute("SELECT COUNT(*) FROM git_operations").fetchone()[0]
                stats["git_commits"] = conn.execute(
                    "SELECT COUNT(*) FROM git_operations WHERE operation_type = 'commit'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pass
            try:
                stats["segments"] = conn.execute(
                    "SELECT COUNT(*) FROM conversation_segments"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                pass
            return stats

    # ─── Projects ──────────────────────────────────────────────────────────

    def upsert_project(self, project: dict[str, Any]) -> None:
        """Insert or update a project row. Merges keywords/aliases on conflict."""
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT keywords_json, aliases_json, session_count, total_edits, first_seen "
                "FROM projects WHERE project_id = ?",
                (project["project_id"],),
            ).fetchone()

            if existing:
                existing_keywords = set(json.loads(existing["keywords_json"]))
                existing_aliases = set(json.loads(existing["aliases_json"]))
                new_keywords = set(project.get("keywords", []))
                new_aliases = set(project.get("aliases", []))
                merged_keywords = sorted(existing_keywords | new_keywords)
                merged_aliases = sorted(existing_aliases | new_aliases)

                conn.execute(
                    """
                    UPDATE projects
                    SET display_name = ?, aliases_json = ?, keywords_json = ?,
                        languages_json = ?, category = ?, last_seen = ?,
                        session_count = session_count + 1,
                        total_edits = total_edits + ?
                    WHERE project_id = ?
                    """,
                    (
                        project.get("display_name"),
                        json.dumps(merged_aliases),
                        json.dumps(merged_keywords),
                        json.dumps(project.get("languages", [])),
                        project.get("category"),
                        project.get("last_seen"),
                        project.get("new_edits", 0),
                        project["project_id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO projects (
                        project_id, canonical_path, display_name, aliases_json,
                        keywords_json, languages_json, category,
                        first_seen, last_seen, session_count, total_edits
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project["project_id"],
                        project["canonical_path"],
                        project["display_name"],
                        json.dumps(project.get("aliases", [])),
                        json.dumps(project.get("keywords", [])),
                        json.dumps(project.get("languages", [])),
                        project.get("category"),
                        project.get("first_seen"),
                        project.get("last_seen"),
                        1,
                        project.get("new_edits", 0),
                    ),
                )

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_projects(
        self,
        keyword: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            conditions: list[str] = []
            params: list[Any] = []
            if category:
                conditions.append("category = ?")
                params.append(category)
            if keyword:
                conditions.append(
                    "(display_name LIKE ? ESCAPE '\\' "
                    "OR keywords_json LIKE ? ESCAPE '\\' "
                    "OR aliases_json LIKE ? ESCAPE '\\')"
                )
                like = f"%{_escape_like(keyword)}%"
                params.extend([like, like, like])

            query = "SELECT * FROM projects"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY last_seen DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def attach_session_to_project(self, session_id: str, project_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE sessions SET project_id = ? WHERE session_id = ?",
                (project_id, session_id),
            )

    # ─── Session outcomes ──────────────────────────────────────────────────

    def upsert_outcome(self, outcome: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO session_outcomes (
                    session_id, outcome, confidence, error_count, fix_count,
                    test_pass_count, test_fail_count,
                    first_error_event_id, resolution_event_id,
                    summary, topics_json, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome["session_id"],
                    outcome["outcome"],
                    outcome.get("confidence", 0.5),
                    outcome.get("error_count", 0),
                    outcome.get("fix_count", 0),
                    outcome.get("test_pass_count", 0),
                    outcome.get("test_fail_count", 0),
                    outcome.get("first_error_event_id"),
                    outcome.get("resolution_event_id"),
                    outcome.get("summary", ""),
                    json.dumps(outcome.get("topics", [])),
                    datetime.now().isoformat(),
                ),
            )

    def get_outcome(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_outcomes WHERE session_id = ?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    # ─── Episodes ──────────────────────────────────────────────────────────

    def insert_episodes(self, episodes: list[dict[str, Any]]) -> int:
        if not episodes:
            return 0
        with self.connect() as conn:
            rows = [
                (
                    ep["episode_id"],
                    ep["session_id"],
                    ep.get("project_id"),
                    ep["started_at"],
                    ep["ended_at"],
                    ep.get("problem_event_id"),
                    ep.get("diagnosis_event_id"),
                    ep.get("fix_event_id"),
                    ep.get("verification_event_id"),
                    ep.get("problem_description", ""),
                    ep.get("diagnosis_summary", ""),
                    ep.get("fix_summary", ""),
                    json.dumps(ep.get("touched_files", [])),
                    json.dumps(ep.get("tags", [])),
                    ep.get("confidence", 0.5),
                    ep.get("status", "unresolved"),
                    ep.get("fix_commit_hash"),
                )
                for ep in episodes
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO episodes (
                    episode_id, session_id, project_id, started_at, ended_at,
                    problem_event_id, diagnosis_event_id, fix_event_id, verification_event_id,
                    problem_description, diagnosis_summary, fix_summary,
                    touched_files_json, tags_json, confidence, status, fix_commit_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            return dict(row) if row else None

    def count_episodes(self) -> int:
        """Fast count of rows in the episodes table."""
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
                return int(row[0]) if row else 0
        except Exception:
            return 0

    def query_episodes(
        self,
        project_ids: list[str] | None = None,
        session_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status: str | None = None,
        keyword: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            conditions: list[str] = []
            params: list[Any] = []

            if project_ids:
                placeholders = ",".join(["?"] * len(project_ids))
                conditions.append(f"project_id IN ({placeholders})")
                params.extend(project_ids)
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if since:
                conditions.append("ended_at >= ?")
                params.append(since)
            if until:
                conditions.append("ended_at <= ?")
                params.append(until)
            if status:
                conditions.append("status = ?")
                params.append(status)
            if keyword:
                conditions.append(
                    "(problem_description LIKE ? ESCAPE '\\' "
                    "OR diagnosis_summary LIKE ? ESCAPE '\\' "
                    "OR fix_summary LIKE ? ESCAPE '\\')"
                )
                like = f"%{_escape_like(keyword)}%"
                params.extend([like, like, like])

            query = "SELECT * FROM episodes"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY ended_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    # ─── Tool pairs ────────────────────────────────────────────────────────

    def upsert_tool_pairs(self, pairs: list[dict[str, Any]]) -> int:
        if not pairs:
            return 0
        with self.connect() as conn:
            rows = [
                (
                    p["tool_use_id"],
                    p["call_event_id"],
                    p.get("result_event_id"),
                    int(p["success"]) if p.get("success") is not None else None,
                    int(p.get("error_detected", False)),
                    p.get("error_snippet"),
                )
                for p in pairs
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO tool_pairs (
                    tool_use_id, call_event_id, result_event_id, success,
                    error_detected, error_snippet
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def get_tool_pair(self, tool_use_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tool_pairs WHERE tool_use_id = ?", (tool_use_id,)
            ).fetchone()
            return dict(row) if row else None

    # ─── Git operations ───────────────────────────────────────────────────

    def insert_git_operations(self, ops: list[dict[str, Any]]) -> int:
        if not ops:
            return 0
        with self.connect() as conn:
            rows = [
                (
                    op["git_op_id"],
                    op["session_id"],
                    op["event_id"],
                    op["operation_type"],
                    op.get("commit_hash"),
                    op.get("commit_message"),
                    op.get("branch"),
                    op.get("remote"),
                    op.get("files_changed_count"),
                    op["timestamp"],
                    int(op.get("success", True)),
                )
                for op in ops
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO git_operations (
                    git_op_id, session_id, event_id, operation_type,
                    commit_hash, commit_message, branch, remote,
                    files_changed_count, timestamp, success
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def get_git_operations(
        self,
        session_id: str,
        operation_type: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            conditions = ["session_id = ?"]
            params: list[Any] = [session_id]
            if operation_type:
                conditions.append("operation_type = ?")
                params.append(operation_type)
            query = "SELECT * FROM git_operations WHERE " + " AND ".join(conditions)
            query += " ORDER BY timestamp ASC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def search_git_operations(
        self,
        query: str,
        session_id: str | None = None,
        operation_type: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            conditions: list[str] = []
            params: list[Any] = []

            if query:
                escaped = _escape_like(query)
                conditions.append(
                    "(commit_message LIKE ? ESCAPE '\\' "
                    "OR commit_hash LIKE ? ESCAPE '\\' "
                    "OR branch LIKE ? ESCAPE '\\')"
                )
                like = f"%{escaped}%"
                params.extend([like, like, like])
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if operation_type:
                conditions.append("operation_type = ?")
                params.append(operation_type)

            sql = "SELECT * FROM git_operations"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_project_git_operations(
        self,
        project_id: str,
        operation_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get git operations across all sessions for a project.

        Joins git_operations with sessions on session_id where
        sessions.project_id matches. Most recent first.
        """
        with self.connect() as conn:
            conditions = ["s.project_id = ?"]
            params: list[Any] = [project_id]
            if operation_type:
                conditions.append("g.operation_type = ?")
                params.append(operation_type)
            sql = (
                "SELECT g.* FROM git_operations g "
                "INNER JOIN sessions s ON g.session_id = s.session_id "
                "WHERE " + " AND ".join(conditions) +
                " ORDER BY g.timestamp DESC LIMIT ?"
            )
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ─── Conversation segments ────────────────────────────────────────────

    def insert_segments(self, segments: list[dict[str, Any]]) -> int:
        """Insert or replace conversation segments."""
        if not segments:
            return 0
        with self.connect() as conn:
            rows = [
                (
                    seg["segment_id"],
                    seg["session_id"],
                    seg.get("project_id"),
                    seg["started_at"],
                    seg["ended_at"],
                    seg["start_sequence"],
                    seg["end_sequence"],
                    seg.get("segment_type", "discussion"),
                    seg.get("topic", ""),
                    seg.get("summary", ""),
                    seg.get("event_count", 0),
                    seg.get("user_message_count", 0),
                    json.dumps(seg.get("keywords", [])),
                )
                for seg in segments
            ]
            conn.executemany(
                """
                INSERT OR REPLACE INTO conversation_segments (
                    segment_id, session_id, project_id, started_at, ended_at,
                    start_sequence, end_sequence, segment_type, topic, summary,
                    event_count, user_message_count, keywords_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def query_segments(
        self,
        project_ids: list[str] | None = None,
        session_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        segment_type: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Query conversation segments with optional filters."""
        with self.connect() as conn:
            conditions: list[str] = []
            params: list[Any] = []

            if project_ids:
                placeholders = ",".join(["?"] * len(project_ids))
                conditions.append(f"project_id IN ({placeholders})")
                params.extend(project_ids)
            if session_id:
                conditions.append("session_id = ?")
                params.append(session_id)
            if since:
                conditions.append("ended_at >= ?")
                params.append(since)
            if until:
                conditions.append("ended_at <= ?")
                params.append(until)
            if segment_type:
                conditions.append("segment_type = ?")
                params.append(segment_type)
            if keyword:
                escaped = _escape_like(keyword)
                conditions.append(
                    "(topic LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')"
                )
                like = f"%{escaped}%"
                params.extend([like, like])

            query = "SELECT * FROM conversation_segments"
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY ended_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]
