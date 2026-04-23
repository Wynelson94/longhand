"""Tests for the schema migration system."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from longhand.storage.migrations import MIGRATIONS, apply_migrations
from longhand.storage.sqlite_store import SQLiteStore


def test_apply_migrations_from_empty(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    # Base schema first (what SQLiteStore does on fresh init)
    conn.execute("CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_path TEXT, transcript_path TEXT, started_at TEXT, ended_at TEXT, event_count INTEGER, user_message_count INTEGER, assistant_message_count INTEGER, tool_call_count INTEGER, file_edit_count INTEGER, git_branch TEXT, cwd TEXT, model TEXT, ingested_at TEXT)")
    conn.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, session_id TEXT, parent_event_id TEXT, event_type TEXT, sequence INTEGER, timestamp TEXT, cwd TEXT, git_branch TEXT, model TEXT, content TEXT, is_sidechain INTEGER, tool_name TEXT, tool_use_id TEXT, tool_input_json TEXT, tool_output TEXT, tool_success INTEGER, file_path TEXT, file_operation TEXT, old_content TEXT, new_content TEXT, raw_json TEXT)")
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
    store.upsert_project({
        "project_id": "proj1",
        "canonical_path": "/tmp/game",
        "display_name": "the game",
        "aliases": ["game", "cosmic"],
        "keywords": ["phaser", "typescript"],
        "languages": ["typescript"],
        "category": "game",
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-04-01T00:00:00Z",
    })
    p = store.get_project("proj1")
    assert p is not None
    assert p["display_name"] == "the game"
    assert p["category"] == "game"

    # Merge on duplicate
    store.upsert_project({
        "project_id": "proj1",
        "canonical_path": "/tmp/game",
        "display_name": "the game",
        "aliases": ["cosmic-defender"],
        "keywords": ["webgl"],
        "languages": ["typescript"],
        "category": "game",
        "last_seen": "2026-04-09T00:00:00Z",
    })
    p2 = store.get_project("proj1")
    import json
    assert "cosmic" in json.loads(p2["aliases_json"])
    assert "cosmic-defender" in json.loads(p2["aliases_json"])
    assert "webgl" in json.loads(p2["keywords_json"])
    assert p2["session_count"] == 2

    # Outcomes
    store.upsert_outcome({
        "session_id": "sess1",
        "outcome": "fixed",
        "confidence": 0.8,
        "error_count": 3,
        "fix_count": 1,
        "summary": "Fixed a race condition",
        "topics": ["race-condition", "stripe"],
    })
    o = store.get_outcome("sess1")
    assert o is not None
    assert o["outcome"] == "fixed"
    assert o["confidence"] == 0.8

    # Episodes
    store.insert_episodes([{
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
    }])
    eps = store.query_episodes(session_id="sess1")
    assert len(eps) == 1
    assert eps[0]["fix_summary"] == "Added mutex guard"

    # Tool pairs
    store.upsert_tool_pairs([{
        "tool_use_id": "toolu_123",
        "call_event_id": "call_evt",
        "result_event_id": "result_evt",
        "success": True,
        "error_detected": False,
    }])
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
    store.insert_episodes([
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
    ])

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
