"""
Schema migrations for Longhand.

Version-aware SQL evolution. Each migration is a SQL string keyed by version.
Migrations are applied in order, once, and logged in the `schema_version` table.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime


MIGRATIONS: dict[int, str] = {
    1: """
    -- v1: proactive memory schema additions

    CREATE TABLE IF NOT EXISTS projects (
        project_id TEXT PRIMARY KEY,
        canonical_path TEXT NOT NULL,
        display_name TEXT NOT NULL,
        aliases_json TEXT NOT NULL,
        keywords_json TEXT NOT NULL,
        languages_json TEXT NOT NULL,
        category TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        session_count INTEGER DEFAULT 0,
        total_edits INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_projects_last_seen ON projects(last_seen DESC);
    CREATE INDEX IF NOT EXISTS idx_projects_category ON projects(category);

    -- sessions.project_id is added by the migration runner as an ALTER
    -- because ALTER TABLE with ADD COLUMN is idempotent-friendly only in newer SQLite.

    CREATE TABLE IF NOT EXISTS session_outcomes (
        session_id TEXT PRIMARY KEY,
        outcome TEXT NOT NULL,
        confidence REAL NOT NULL,
        error_count INTEGER DEFAULT 0,
        fix_count INTEGER DEFAULT 0,
        test_pass_count INTEGER DEFAULT 0,
        test_fail_count INTEGER DEFAULT 0,
        first_error_event_id TEXT,
        resolution_event_id TEXT,
        summary TEXT NOT NULL,
        topics_json TEXT NOT NULL,
        computed_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON session_outcomes(outcome);

    CREATE TABLE IF NOT EXISTS episodes (
        episode_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        project_id TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT NOT NULL,
        problem_event_id TEXT,
        diagnosis_event_id TEXT,
        fix_event_id TEXT,
        verification_event_id TEXT,
        problem_description TEXT,
        diagnosis_summary TEXT,
        fix_summary TEXT,
        touched_files_json TEXT,
        tags_json TEXT,
        confidence REAL DEFAULT 0.5,
        status TEXT DEFAULT 'unresolved'
    );

    CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id);
    CREATE INDEX IF NOT EXISTS idx_episodes_session ON episodes(session_id);
    CREATE INDEX IF NOT EXISTS idx_episodes_ended_at ON episodes(ended_at DESC);
    CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);

    CREATE TABLE IF NOT EXISTS tool_pairs (
        tool_use_id TEXT PRIMARY KEY,
        call_event_id TEXT NOT NULL,
        result_event_id TEXT,
        success INTEGER,
        error_detected INTEGER DEFAULT 0,
        error_snippet TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_tool_pairs_call ON tool_pairs(call_event_id);
    CREATE INDEX IF NOT EXISTS idx_tool_pairs_error ON tool_pairs(error_detected) WHERE error_detected = 1;
    """,
}


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _apply_alters(conn: sqlite3.Connection, version: int) -> None:
    """ALTER TABLE operations that need guarding (not idempotent across SQLite versions)."""
    if version == 1:
        if not _column_exists(conn, "sessions", "project_id"):
            conn.execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")
        if not _column_exists(conn, "events", "error_detected"):
            conn.execute("ALTER TABLE events ADD COLUMN error_detected INTEGER DEFAULT 0")
        if not _column_exists(conn, "events", "error_snippet"):
            conn.execute("ALTER TABLE events ADD COLUMN error_snippet TEXT")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {r[0] for r in rows}


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply any unapplied migrations. Returns the list of versions applied this run."""
    _ensure_version_table(conn)
    applied = _applied_versions(conn)

    newly_applied: list[int] = []
    for version in sorted(MIGRATIONS.keys()):
        if version in applied:
            continue

        sql = MIGRATIONS[version]
        conn.executescript(sql)
        _apply_alters(conn, version)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now().isoformat()),
        )
        newly_applied.append(version)

    conn.commit()
    return newly_applied
