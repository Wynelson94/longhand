"""Promise 2 (forward data compatibility) — the enforcement artifact.

COMPATIBILITY.md promises that any database written by 0.11+ opens on any 1.x
at least as new as its last writer, with migrations applied automatically and
no data loss. This module is what makes that a tested claim rather than a
hopeful one.

The fixture is a GENUINE v0.11.2 schema, not a hand-written approximation.
Hand-written schemas drift from what actually shipped and prove nothing, so
`tests/fixtures/db/v0_11_2_schema.sql` was produced by executing that tag's
own code. To regenerate it after a new baseline is chosen:

    SCHEMA = re.search(r'^SCHEMA = \"\"\"(.*?)\"\"\"',
                       git_show("<tag>:longhand/storage/sqlite_store.py"),
                       re.S | re.M).group(1)
    ns = {}; exec(git_show("<tag>:longhand/storage/migrations.py"), ns)
    conn.executescript(SCHEMA)
    ns["apply_migrations"](conn)     # the tag's real code path, _apply_alters included
    # ...seed representative rows, then conn.iterdump()

Note that v0.11.2 predates `MAX_KNOWN_MIGRATION` — the downgrade guard arrived
in 0.13. That absence is part of what makes this a real 0.11 database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from longhand.storage import LonghandStore
from longhand.storage.migrations import MAX_KNOWN_MIGRATION

FIXTURE = Path(__file__).parent / "fixtures" / "db" / "v0_11_2_schema.sql"

# What the v0.11.2 fixture ships with. Migrations 7+ are what a 1.x release
# must apply on top without losing anything.
V0_11_MIGRATIONS = [1, 2, 3, 4, 5, 6]
SESSION_ID = "11111111-2222-3333-4444-555555555555"
PROJECT_ID = "p_fixture0001"


def _applied(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY version")]
    finally:
        conn.close()


@pytest.fixture
def v011_data_dir(tmp_path: Path) -> Path:
    """A data dir holding an un-migrated v0.11.2 database."""
    data_dir = tmp_path / "longhand"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "longhand.db")
    conn.executescript(FIXTURE.read_text())
    conn.commit()
    conn.close()
    return data_dir


def test_fixture_really_is_a_v0_11_database(v011_data_dir: Path):
    """Guard the guard: if the fixture drifts forward, the rest proves nothing."""
    assert _applied(v011_data_dir / "longhand.db") == V0_11_MIGRATIONS
    assert V0_11_MIGRATIONS[-1] < MAX_KNOWN_MIGRATION, (
        "fixture is not actually behind current — pick an older baseline"
    )


def test_v0_11_database_opens_and_migrates_automatically(v011_data_dir: Path):
    """Promise 2: no command to run, no flag to pass — it just opens."""
    LonghandStore(data_dir=v011_data_dir)

    applied = _applied(v011_data_dir / "longhand.db")
    assert applied == list(range(1, MAX_KNOWN_MIGRATION + 1))
    # The migrations the 0.11 database was missing actually ran.
    assert set(applied) - set(V0_11_MIGRATIONS)


def test_migration_preserves_the_0_11_data(v011_data_dir: Path):
    """Promise 2 is about data, not just DDL. Nothing may be dropped."""
    store = LonghandStore(data_dir=v011_data_dir)

    with store.sqlite.connect() as conn:
        sessions = conn.execute(
            "SELECT session_id, project_id, event_count FROM sessions"
        ).fetchall()
        events = conn.execute("SELECT event_id, content FROM events ORDER BY sequence").fetchall()
        project = conn.execute(
            "SELECT display_name FROM projects WHERE project_id = ?", (PROJECT_ID,)
        ).fetchone()

    assert [tuple(r) for r in sessions] == [(SESSION_ID, PROJECT_ID, 2)]
    assert [r[0] for r in events] == ["ev_fixture_0001", "ev_fixture_0002"]
    assert [r[1] for r in events] == ["the fixture question", "edited the file"]
    assert project[0] == "fixture-project"


def test_core_queries_work_against_a_migrated_0_11_store(v011_data_dir: Path):
    """A migrated database must be usable, not merely openable."""
    store = LonghandStore(data_dir=v011_data_dir)

    listed = store.sqlite.list_sessions(limit=10)
    assert any(s["session_id"] == SESSION_ID for s in listed)

    events = store.sqlite.get_events(SESSION_ID)
    assert len(events) == 2

    history = store.sqlite.get_events(file_path="/tmp/fixture-project/main.py")
    assert [e["event_id"] for e in history] == ["ev_fixture_0002"]

    # The ReplayEngine reads through the same migrated schema.
    from longhand.replay import ReplayEngine

    edits = ReplayEngine(store.sqlite).file_history("/tmp/fixture-project/main.py")
    assert [e["event_id"] for e in edits] == ["ev_fixture_0002"]


def test_migration_is_one_time(v011_data_dir: Path):
    """Re-opening applies nothing further and changes nothing (rule 2)."""
    LonghandStore(data_dir=v011_data_dir)
    first = _applied(v011_data_dir / "longhand.db")

    LonghandStore(data_dir=v011_data_dir)
    assert _applied(v011_data_dir / "longhand.db") == first
    # No duplicate rows from a second pass.
    assert len(first) == len(set(first))
