"""Tests for the local backup state file and the cheap change filter.

`backup-state.json` lives in the data dir, never in the repo. It records per
session the triple `(event_count, last_event_timestamp, ingested_at)` as of
the last run, plus the sha256 the repo holds; a session is re-exported only
when its triple moved.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from longhand.backup.state import (
    STATE_FILENAME,
    BackupState,
    SessionMark,
    changed_sessions,
    session_marks,
)
from longhand.parser import JSONLParser
from longhand.types import Event, EventType


def _ingest(store: Any, path: Path) -> None:
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)


def _event(event_id: str, session_id: str, minute: int) -> Event:
    return Event(
        event_id=event_id,
        session_id=session_id,
        event_type=EventType.USER_MESSAGE,
        sequence=900 + minute,
        timestamp=datetime(2026, 4, 9, 10, minute, tzinfo=timezone.utc),
        content="one more",
        raw={"type": "user", "uuid": event_id},
    )


# ─── State file ───────────────────────────────────────────────────────────────


def test_state_load_missing_is_empty(tmp_path):
    state = BackupState.load(tmp_path)
    assert state.sessions == {}
    assert state.last_run_at is None
    assert state.last_push_at is None
    assert state.last_error is None
    assert state.last_mask_counts == {}
    assert state.last_run_summary == {}


def test_state_load_corrupt_is_empty(tmp_path):
    (tmp_path / STATE_FILENAME).write_text("{nope", encoding="utf-8")
    state = BackupState.load(tmp_path)
    assert state.last_run_at is None
    assert state.sessions == {}


def test_state_load_wrong_shape_is_empty(tmp_path):
    (tmp_path / STATE_FILENAME).write_text('{"sessions": [1, 2]}', encoding="utf-8")
    assert BackupState.load(tmp_path).sessions == {}


def test_state_save_round_trip_and_mode(tmp_path):
    state = BackupState(
        sessions={
            "s1": SessionMark(
                event_count=5,
                last_event_timestamp="2026-04-09T10:00:04+00:00",
                ingested_at="2026-04-09T11:00:00+00:00",
                sha256="ab" * 32,
            ),
            "s2": SessionMark(event_count=1, last_event_timestamp=None, ingested_at=None),
        },
        last_run_at="2026-10-29T00:00:00+00:00",
        last_push_at="2026-10-29T00:00:01+00:00",
        last_error="push failed",
        last_mask_counts={"github_token": 1},
        last_run_summary={"added": 1, "grown": 0},
    )
    state.save(tmp_path)
    assert BackupState.load(tmp_path) == state
    assert sorted(p.name for p in tmp_path.iterdir()) == [STATE_FILENAME]
    if sys.platform != "win32":
        assert (tmp_path / STATE_FILENAME).stat().st_mode & 0o777 == 0o600


def test_state_save_overwrites_atomically(tmp_path):
    BackupState(last_run_at="first").save(tmp_path)
    BackupState(last_run_at="second").save(tmp_path)
    assert BackupState.load(tmp_path).last_run_at == "second"
    assert sorted(p.name for p in tmp_path.iterdir()) == [STATE_FILENAME]


def test_state_default_containers_are_not_shared():
    assert BackupState().sessions is not BackupState().sessions
    assert BackupState().last_mask_counts is not BackupState().last_mask_counts


# ─── Marks ────────────────────────────────────────────────────────────────────


def test_session_mark_changed_from_ignores_sha():
    mark = SessionMark(5, "t1", "i1", "sha-a")
    assert not mark.changed_from(SessionMark(5, "t1", "i1", "sha-b"))
    assert mark.changed_from(SessionMark(6, "t1", "i1", "sha-a"))
    assert mark.changed_from(SessionMark(5, "t2", "i1", "sha-a"))
    assert mark.changed_from(SessionMark(5, "t1", "i2", "sha-a"))


def test_session_mark_dict_round_trip():
    mark = SessionMark(5, "t1", "i1", "sha-a")
    assert SessionMark.from_dict(mark.to_dict()) == mark
    assert SessionMark.from_dict({"event_count": "3"}) == SessionMark(3, None, None, None)


def test_session_marks_counts_events_and_skips_empty_session_id(temp_store, sample_session_file):
    _ingest(temp_store, sample_session_file)
    # A '' session row with an orphan event, as older parsers left behind.
    with temp_store.sqlite.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, transcript_path, started_at, ended_at, ingested_at)"
            " VALUES ('', '', '1970-01-01T00:00:00+00:00', '1970-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')"
        )
    temp_store.sqlite.insert_events([_event("orphan-1", "", 7)])

    marks = session_marks(temp_store.sqlite)
    assert set(marks) == {"test-session-1"}
    row = temp_store.sqlite.get_session("test-session-1")
    mark = marks["test-session-1"]
    assert mark.event_count == row["event_count"]
    assert mark.ingested_at == row["ingested_at"]
    assert mark.last_event_timestamp.startswith("2026-04-09T10:00:04")
    assert mark.sha256 is None


def test_session_marks_empty_store(temp_store):
    assert session_marks(temp_store.sqlite) == {}


# ─── Change filter ────────────────────────────────────────────────────────────


def test_changed_sessions_new_then_unchanged_then_changed(
    temp_store, sample_session_file, tmp_path
):
    _ingest(temp_store, sample_session_file)
    state = BackupState.load(tmp_path)
    marks = session_marks(temp_store.sqlite)
    assert changed_sessions(marks, state) == ["test-session-1"]

    state.sessions = dict(marks)
    assert changed_sessions(marks, state) == []

    # One more event row for the session moves the triple: changed again.
    temp_store.sqlite.insert_events([_event("user-99", "test-session-1", 5)])
    assert changed_sessions(session_marks(temp_store.sqlite), state) == ["test-session-1"]


def test_changed_sessions_sorted_and_ignores_sessions_gone_from_the_store():
    state = BackupState(sessions={"gone": SessionMark(1, None, None)})
    current = {"b": SessionMark(1, None, None), "a": SessionMark(2, None, None)}
    assert changed_sessions(current, state) == ["a", "b"]


def test_changed_sessions_sha_alone_does_not_trigger():
    state = BackupState(sessions={"s": SessionMark(1, "t", "i", "old-sha")})
    assert changed_sessions({"s": SessionMark(1, "t", "i", None)}, state) == []
