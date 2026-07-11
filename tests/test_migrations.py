"""Tests for the schema migration system."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from longhand.storage.migrations import (
    MAX_KNOWN_MIGRATION,
    MIGRATIONS,
    SchemaTooNewError,
    apply_migrations,
)
from longhand.storage.sqlite_store import SQLiteStore


def test_apply_migrations_from_empty(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    # Base schema first (what SQLiteStore does on fresh init)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_path TEXT, transcript_path TEXT, started_at TEXT, ended_at TEXT, event_count INTEGER, user_message_count INTEGER, assistant_message_count INTEGER, tool_call_count INTEGER, file_edit_count INTEGER, git_branch TEXT, cwd TEXT, model TEXT, ingested_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, session_id TEXT, parent_event_id TEXT, event_type TEXT, sequence INTEGER, timestamp TEXT, cwd TEXT, git_branch TEXT, model TEXT, content TEXT, is_sidechain INTEGER, tool_name TEXT, tool_use_id TEXT, tool_input_json TEXT, tool_output TEXT, tool_success INTEGER, file_path TEXT, file_operation TEXT, old_content TEXT, new_content TEXT, raw_json TEXT)"
    )
    conn.commit()

    applied = apply_migrations(conn)
    assert applied == sorted(MIGRATIONS.keys())

    # New tables must exist
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "projects" in tables
    assert "session_outcomes" in tables
    assert "episodes" in tables
    assert "tool_pairs" in tables
    assert "schema_version" in tables

    # project_id column added to sessions
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert "project_id" in cols

    # error columns added to events
    event_cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    assert "error_detected" in event_cols
    assert "error_snippet" in event_cols

    conn.close()


def test_migrations_idempotent(tmp_path: Path):
    """Running migrations twice should be a no-op on the second pass."""
    store = SQLiteStore(tmp_path / "idempotent.db")

    with store.connect() as conn:
        first = apply_migrations(conn)
        second = apply_migrations(conn)

    assert first == []  # already applied during SQLiteStore init
    assert second == []


def test_new_crud_roundtrip(tmp_path: Path):
    """Exercise the new project/outcome/episode/tool_pair CRUD helpers."""
    store = SQLiteStore(tmp_path / "crud.db")

    # Projects
    store.upsert_project(
        {
            "project_id": "proj1",
            "canonical_path": "/tmp/game",
            "display_name": "the game",
            "aliases": ["game", "cosmic"],
            "keywords": ["phaser", "typescript"],
            "languages": ["typescript"],
            "category": "game",
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-04-01T00:00:00Z",
        }
    )
    p = store.get_project("proj1")
    assert p is not None
    assert p["display_name"] == "the game"
    assert p["category"] == "game"

    # Merge on duplicate
    store.upsert_project(
        {
            "project_id": "proj1",
            "canonical_path": "/tmp/game",
            "display_name": "the game",
            "aliases": ["cosmic-defender"],
            "keywords": ["webgl"],
            "languages": ["typescript"],
            "category": "game",
            "last_seen": "2026-04-09T00:00:00Z",
        }
    )
    p2 = store.get_project("proj1")
    import json

    assert "cosmic" in json.loads(p2["aliases_json"])
    assert "cosmic-defender" in json.loads(p2["aliases_json"])
    assert "webgl" in json.loads(p2["keywords_json"])
    # upsert_project no longer increments session_count per call — the count is
    # recomputed from the sessions table (recompute_project_stats). With no
    # sessions attached to this project, it stays 0 no matter how many times the
    # project metadata is upserted.
    assert p2["session_count"] == 0

    # Outcomes
    store.upsert_outcome(
        {
            "session_id": "sess1",
            "outcome": "fixed",
            "confidence": 0.8,
            "error_count": 3,
            "fix_count": 1,
            "summary": "Fixed a race condition",
            "topics": ["race-condition", "stripe"],
        }
    )
    o = store.get_outcome("sess1")
    assert o is not None
    assert o["outcome"] == "fixed"
    assert o["confidence"] == 0.8

    # Episodes
    store.insert_episodes(
        [
            {
                "episode_id": "ep1",
                "session_id": "sess1",
                "project_id": "proj1",
                "started_at": "2026-04-01T10:00:00Z",
                "ended_at": "2026-04-01T10:30:00Z",
                "problem_event_id": "evt1",
                "fix_event_id": "evt2",
                "problem_description": "Tests were failing with race condition",
                "fix_summary": "Added mutex guard",
                "touched_files": ["/tmp/game/state.ts"],
                "tags": ["bug-fix", "race-condition"],
                "status": "resolved",
            }
        ]
    )
    eps = store.query_episodes(session_id="sess1")
    assert len(eps) == 1
    assert eps[0]["fix_summary"] == "Added mutex guard"

    # Tool pairs
    store.upsert_tool_pairs(
        [
            {
                "tool_use_id": "toolu_123",
                "call_event_id": "call_evt",
                "result_event_id": "result_evt",
                "success": True,
                "error_detected": False,
            }
        ]
    )
    pair = store.get_tool_pair("toolu_123")
    assert pair is not None
    assert pair["success"] == 1


def test_migration_v4_strips_intent_prefix_from_fix_summary(tmp_path: Path):
    """v0.8 migration strips the leaked 'Intent: ' label from existing fix_summary rows.

    Anchored to the 2026-04-23 audit of /Users/natenelson/.longhand where
    100 of 204 episodes had fix_summary starting with "Intent:" because
    pre-v0.8 _compose_fix_summary prepended the label "so the embedding
    treats it structurally" (per the original comment). The label leaked
    into the user-visible narrative on every recall.
    """
    store = SQLiteStore(tmp_path / "intent.db")
    store.insert_episodes(
        [
            {
                "episode_id": "ep_dirty_1",
                "session_id": "s1",
                "project_id": "p1",
                "started_at": "2026-04-01T10:00:00Z",
                "ended_at": "2026-04-01T10:30:00Z",
                "problem_event_id": "ev1",
                "fix_event_id": "ev2",
                "problem_description": "something broke",
                "fix_summary": "Intent: I'll patch the thing. Edit on x.py: 'a' → 'b'",
                "touched_files": [],
                "tags": [],
                "status": "resolved",
            },
            {
                "episode_id": "ep_dirty_2",
                "session_id": "s1",
                "project_id": "p1",
                "started_at": "2026-04-01T11:00:00Z",
                "ended_at": "2026-04-01T11:30:00Z",
                "problem_event_id": "ev3",
                "fix_event_id": "ev4",
                "problem_description": "another thing broke",
                "fix_summary": "Intent: Let me fix it. Write on y.py",
                "touched_files": [],
                "tags": [],
                "status": "resolved",
            },
            {
                "episode_id": "ep_clean",
                "session_id": "s1",
                "project_id": "p1",
                "started_at": "2026-04-01T12:00:00Z",
                "ended_at": "2026-04-01T12:30:00Z",
                "problem_event_id": "ev5",
                "fix_event_id": "ev6",
                "problem_description": "this one was already clean",
                "fix_summary": "I'll fix it. Edit on z.py",
                "touched_files": [],
                "tags": [],
                "status": "resolved",
            },
        ]
    )

    # Simulate a pre-v2 DB by rolling the schema_version back before applying.
    with store.connect() as conn:
        conn.execute("DELETE FROM schema_version WHERE version = 4")
        conn.commit()
        applied = apply_migrations(conn)

    assert 4 in applied

    # Dirty rows cleaned; clean row untouched.
    dirty1 = store.get_episode("ep_dirty_1")
    dirty2 = store.get_episode("ep_dirty_2")
    clean = store.get_episode("ep_clean")

    assert dirty1["fix_summary"].startswith("I'll patch")
    assert not dirty1["fix_summary"].startswith("Intent:")
    assert dirty2["fix_summary"].startswith("Let me fix")
    assert not dirty2["fix_summary"].startswith("Intent:")
    assert clean["fix_summary"].startswith("I'll fix")


def _insert_session(conn, session_id: str, project_id: str | None, file_edits: int) -> None:
    """Insert a minimal sessions row for project-rollup tests."""
    conn.execute(
        "INSERT INTO sessions (session_id, transcript_path, started_at, ended_at, "
        "ingested_at, file_edit_count, project_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            f"/tmp/{session_id}.jsonl",
            "2026-04-01T00:00:00Z",
            "2026-04-01T00:00:00Z",
            "2026-04-01T00:00:00Z",
            file_edits,
            project_id,
        ),
    )


def _make_project(store: SQLiteStore, project_id: str = "proj1") -> None:
    store.upsert_project(
        {
            "project_id": project_id,
            "canonical_path": f"/tmp/{project_id}",
            "display_name": project_id,
            "aliases": [],
            "keywords": [],
            "languages": [],
            "category": None,
            "first_seen": "2026-04-01T00:00:00Z",
            "last_seen": "2026-04-01T00:00:00Z",
        }
    )


def test_recompute_project_stats_counts_distinct_sessions(tmp_path: Path):
    """session_count / total_edits are recomputed from the sessions table:
    distinct attached sessions and the SUM of their file_edit_count."""
    store = SQLiteStore(tmp_path / "recompute.db")
    _make_project(store)
    _make_project(store, "other")
    with store.connect() as conn:
        _insert_session(conn, "s1", "proj1", file_edits=3)
        _insert_session(conn, "s2", "proj1", file_edits=2)
        # A session attached to a different project must not leak into proj1.
        _insert_session(conn, "s3", "other", file_edits=10)
        conn.commit()

    store.recompute_project_stats("proj1")
    p = store.get_project("proj1")
    assert p["session_count"] == 2
    assert p["total_edits"] == 5

    # Idempotent — recomputing again does not change the authoritative counts.
    store.recompute_project_stats("proj1")
    p = store.get_project("proj1")
    assert p["session_count"] == 2
    assert p["total_edits"] == 5


def test_v6_migration_repairs_inflated_project_counts(tmp_path: Path):
    """The v6 backfill recomputes inflated session_count / total_edits from the
    sessions table for existing databases.

    Regression for the counter-inflation bug: upsert_project() incremented both
    columns on every (re-)ingest, so they counted ingest events, not sessions.
    """
    store = SQLiteStore(tmp_path / "repair.db")
    _make_project(store)
    with store.connect() as conn:
        _insert_session(conn, "s1", "proj1", file_edits=4)
        _insert_session(conn, "s2", "proj1", file_edits=1)
        # Simulate the old inflation, and pretend v6 has not run yet.
        conn.execute("UPDATE projects SET session_count = 99, total_edits = 999")
        conn.execute("DELETE FROM schema_version WHERE version = 6")
        conn.commit()
        applied = apply_migrations(conn)

    assert 6 in applied
    p = store.get_project("proj1")
    assert p["session_count"] == 2
    assert p["total_edits"] == 5


def test_migration_race_loser_recovers(tmp_path: Path):
    """A process that loses the post-upgrade migration race must not crash.

    Simulate the loser: its first read of applied versions is stale (empty),
    but the winner has already fully applied every migration. The loser's
    DDL/INSERT conflicts must be swallowed once the version rows are visible.
    """
    from unittest.mock import patch

    from longhand.storage import migrations as mig

    store = SQLiteStore(tmp_path / "race.db")  # fully migrated on init

    real = mig._applied_versions
    calls = {"n": 0}

    def stale_first(conn):
        calls["n"] += 1
        return set() if calls["n"] == 1 else real(conn)

    with (
        store.connect() as conn,
        patch.object(mig, "_applied_versions", side_effect=stale_first),
    ):
        applied = apply_migrations(conn)

    assert applied == []  # loser records nothing; the winner's rows stand


def test_concurrent_store_init_does_not_crash(tmp_path: Path):
    """Two stores constructed concurrently on one fresh DB must both survive.

    Stand-in for parallel session hooks racing migrations right after an
    upgrade — each thread opens its own connection, like separate processes.
    """
    import threading

    db = tmp_path / "concurrent.db"
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def build() -> None:
        try:
            barrier.wait(timeout=10)
            SQLiteStore(db)
        except Exception as e:  # pragma: no cover — the bug this guards against
            errors.append(e)

    threads = [threading.Thread(target=build) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == []
    conn = sqlite3.connect(str(db))
    dupes = conn.execute(
        "SELECT version FROM schema_version GROUP BY version HAVING COUNT(*) > 1"
    ).fetchall()
    conn.close()
    assert dupes == []


def test_v7_adds_analysis_stage_null_for_existing_rows(tmp_path: Path):
    """Upgrading a pre-v7 database adds the column with NULL for existing
    rows — NULL means "unknown, treat as complete" so the upgrade never
    triggers a re-analysis stampede."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    # Minimal base tables so migrations 1-6 apply (matches
    # test_apply_migrations_from_empty), plus the pre-v7 ingestion_log shape.
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_path TEXT, transcript_path TEXT, started_at TEXT, ended_at TEXT, event_count INTEGER, user_message_count INTEGER, assistant_message_count INTEGER, tool_call_count INTEGER, file_edit_count INTEGER, git_branch TEXT, cwd TEXT, model TEXT, ingested_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (event_id TEXT PRIMARY KEY, session_id TEXT, parent_event_id TEXT, event_type TEXT, sequence INTEGER, timestamp TEXT, cwd TEXT, git_branch TEXT, model TEXT, content TEXT, is_sidechain INTEGER, tool_name TEXT, tool_use_id TEXT, tool_input_json TEXT, tool_output TEXT, tool_success INTEGER, file_path TEXT, file_operation TEXT, old_content TEXT, new_content TEXT, raw_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE ingestion_log (transcript_path TEXT PRIMARY KEY, "
        "session_id TEXT NOT NULL, ingested_at TEXT NOT NULL, "
        "file_size INTEGER NOT NULL, event_count INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO ingestion_log VALUES ('/t/old.jsonl', 's-old', '2026-01-01', 100, 5)")
    conn.commit()

    apply_migrations(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(ingestion_log)")}
    assert "analysis_stage" in cols
    stage = conn.execute(
        "SELECT analysis_stage FROM ingestion_log WHERE transcript_path = '/t/old.jsonl'"
    ).fetchone()[0]
    assert stage is None
    conn.close()


def test_fresh_store_has_analysis_stage(tmp_path: Path):
    store = SQLiteStore(tmp_path / "lh" / "longhand.db")
    with store.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ingestion_log)")}
    assert "analysis_stage" in cols


def test_v8_strips_ask_prefix_from_problem_descriptions(tmp_path: Path):
    """Migration 8 removes the leaked 'Ask: ' scaffold from stored episodes
    (same class as the v4 'Intent: ' strip)."""
    store = SQLiteStore(tmp_path / "lh" / "longhand.db")
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO episodes (episode_id, session_id, started_at, ended_at,"
            " problem_event_id, problem_description, confidence, status)"
            " VALUES ('ep-a', 's1', '2026-01-01', '2026-01-01', 'pe',"
            " 'Ask: fix the bug. Error: boom', 0.8, 'resolved'),"
            " ('ep-b', 's1', '2026-01-01', '2026-01-01', 'pe',"
            " 'Task keeps failing', 0.8, 'resolved')"
        )
        # Roll back v8 so we can watch it apply to the seeded rows.
        conn.execute("DELETE FROM schema_version WHERE version = 8")
        conn.execute(
            "UPDATE episodes SET problem_description = 'Ask: fix the bug. Error: boom'"
            " WHERE episode_id = 'ep-a'"
        )
        apply_migrations(conn)
        rows = dict(conn.execute("SELECT episode_id, problem_description FROM episodes").fetchall())
    assert rows["ep-a"] == "fix the bug. Error: boom"
    assert rows["ep-b"] == "Task keeps failing"  # untouched


# ─── downgrade guard: refuse databases written by a newer longhand ────────────


def test_apply_migrations_refuses_too_new_schema(tmp_path: Path):
    """A DB stamped by a newer longhand refuses loudly instead of operating blind."""
    conn = sqlite3.connect(str(tmp_path / "future.db"))
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (MAX_KNOWN_MIGRATION + 1, "2027-01-01T00:00:00Z"),
    )
    conn.commit()

    with pytest.raises(SchemaTooNewError) as excinfo:
        apply_migrations(conn)

    msg = str(excinfo.value)
    assert "written by a newer longhand" in msg
    assert "pip install -U longhand" in msg
    conn.close()


def test_store_construction_refuses_too_new_db(tmp_path: Path):
    """The guard covers every construction path — SQLiteStore init raises too."""
    db = tmp_path / "future-store.db"
    store = SQLiteStore(db)
    with store.connect() as conn:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (MAX_KNOWN_MIGRATION + 1, "2027-01-01T00:00:00Z"),
        )
        conn.commit()

    with pytest.raises(SchemaTooNewError):
        SQLiteStore(db)


def test_migration_stamps_are_utc_aware(tmp_path: Path):
    """schema_version.applied_at carries a UTC offset (v0.13 normalization);
    legacy naive stamps are read as UTC — metadata-grade, no backfill."""
    from datetime import datetime

    store = SQLiteStore(tmp_path / "aware.db")
    with store.connect() as conn:
        stamps = [r[0] for r in conn.execute("SELECT applied_at FROM schema_version")]

    assert stamps
    for stamp in stamps:
        assert datetime.fromisoformat(stamp).tzinfo is not None, stamp


def test_equal_version_db_reopens_fine(tmp_path: Path):
    """A DB at exactly the newest known version opens without complaint."""
    db = tmp_path / "current.db"
    SQLiteStore(db)

    store = SQLiteStore(db)  # second open: fully migrated, must not raise
    with store.connect() as conn:
        newest = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert newest == MAX_KNOWN_MIGRATION
