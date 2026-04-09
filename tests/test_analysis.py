"""Tests for the analysis layer (project inference, outcomes, episodes)."""

from __future__ import annotations

from datetime import datetime, timezone

from longhand.analysis.episode_extraction import extract_episodes
from longhand.analysis.outcomes import classify_session
from longhand.analysis.project_inference import infer_project
from longhand.types import Event, EventType, FileOperation, Session


def _session(cwd: str = "/tmp/cosmic-game") -> Session:
    return Session(
        session_id="test-session",
        project_path=cwd,
        transcript_path="/tmp/fake.jsonl",
        started_at=datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 4, 9, 11, 0, 0, tzinfo=timezone.utc),
        event_count=0,
        git_branch="main",
        cwd=cwd,
        model="claude-sonnet-4-6",
    )


def _event(
    event_id: str,
    event_type: EventType,
    sequence: int,
    content: str = "",
    tool_name: str | None = None,
    file_path: str | None = None,
    file_operation: FileOperation | None = None,
    error_detected: bool = False,
    error_category: str | None = None,
    tool_use_id: str | None = None,
    old_content: str | None = None,
    new_content: str | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        session_id="test-session",
        event_type=event_type,
        sequence=sequence,
        timestamp=datetime(2026, 4, 9, 10, sequence, 0, tzinfo=timezone.utc),
        content=content,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        file_path=file_path,
        file_operation=file_operation,
        old_content=old_content,
        new_content=new_content,
        error_detected=error_detected,
        error_category=error_category,
        error_snippet=content[:200] if error_detected else None,
    )


# ─── Project inference ─────────────────────────────────────────────────────

def test_infer_project_basic():
    session = _session("/tmp/cosmic-game")
    events = [
        _event("e1", EventType.USER_MESSAGE, 1, "I'm building a game with phaser"),
        _event("e2", EventType.TOOL_CALL, 2, tool_name="Edit", file_path="/tmp/cosmic-game/src/main.ts", file_operation=FileOperation.EDIT),
    ]
    project = infer_project(session, events)
    assert project["display_name"] == "cosmic game"
    assert "game" in project["aliases"] or "cosmic" in project["aliases"]
    assert "typescript" in project["languages"]
    assert project["category"] == "game"


def test_infer_project_category_from_keywords():
    session = _session("/tmp/my-thing")
    events = [
        _event("e1", EventType.USER_MESSAGE, 1, "Fix the phaser sprite rendering bug"),
        _event("e2", EventType.TOOL_CALL, 2, tool_name="Edit", file_path="/tmp/my-thing/game.ts", file_operation=FileOperation.EDIT),
    ]
    project = infer_project(session, events)
    assert project["category"] == "game"


# ─── Outcome classification ────────────────────────────────────────────────

def test_classify_fixed_session():
    session = _session()
    events = [
        _event("u1", EventType.USER_MESSAGE, 1, "Run the tests"),
        _event("c1", EventType.TOOL_CALL, 2, tool_name="Bash"),
        _event("r1", EventType.TOOL_RESULT, 3, content="FAIL tests/test_foo.py", error_detected=True, error_category="test"),
        _event("t1", EventType.ASSISTANT_THINKING, 4, "The test failed because of a null check"),
        _event("c2", EventType.TOOL_CALL, 5, tool_name="Edit", file_path="/tmp/foo.py", file_operation=FileOperation.EDIT),
        _event("r2", EventType.TOOL_RESULT, 6, content="All tests passed"),
    ]
    outcome = classify_session(session, events)
    assert outcome["outcome"] == "fixed"
    assert outcome["error_count"] == 1
    assert outcome["first_error_event_id"] == "r1"
    assert outcome["resolution_event_id"] == "r2"


def test_classify_stuck_session():
    session = _session()
    events = [
        _event("u1", EventType.USER_MESSAGE, 1, "Debug this"),
        _event("c1", EventType.TOOL_CALL, 2, tool_name="Bash"),
        _event("r1", EventType.TOOL_RESULT, 3, content="error: something broke", error_detected=True),
        _event("c2", EventType.TOOL_CALL, 4, tool_name="Bash"),
        _event("r2", EventType.TOOL_RESULT, 5, content="error: still broken", error_detected=True),
    ]
    outcome = classify_session(session, events)
    assert outcome["outcome"] == "stuck"


def test_classify_shipped_session():
    session = _session()
    events = [
        _event("u1", EventType.USER_MESSAGE, 1, "Add the feature"),
        _event("c1", EventType.TOOL_CALL, 2, tool_name="Edit", file_path="/tmp/a.ts", file_operation=FileOperation.EDIT),
        _event("c2", EventType.TOOL_CALL, 3, tool_name="Edit", file_path="/tmp/b.ts", file_operation=FileOperation.EDIT),
        _event("c3", EventType.TOOL_CALL, 4, tool_name="Edit", file_path="/tmp/c.ts", file_operation=FileOperation.EDIT),
        _event("r1", EventType.TOOL_RESULT, 5, content="ok"),
    ]
    outcome = classify_session(session, events)
    assert outcome["outcome"] == "shipped"


# ─── Episode extraction ────────────────────────────────────────────────────

def test_extract_episode_with_full_chain():
    events = [
        _event("u1", EventType.USER_MESSAGE, 1, "Why are the tests failing?"),
        _event("c1", EventType.TOOL_CALL, 2, tool_name="Bash"),
        _event("r1", EventType.TOOL_RESULT, 3, content="FAIL /tmp/game/src/main.ts: TypeError", error_detected=True, error_category="test"),
        _event("t1", EventType.ASSISTANT_THINKING, 4, "Looking at main.ts there's a null check missing"),
        _event("c2", EventType.TOOL_CALL, 5, tool_name="Edit", file_path="/tmp/game/src/main.ts", file_operation=FileOperation.EDIT, old_content="x.foo()", new_content="x?.foo()"),
        _event("r2", EventType.TOOL_RESULT, 6, content="All tests passed"),
    ]
    episodes = extract_episodes("sess1", "proj1", events)
    assert len(episodes) == 1
    ep = episodes[0]
    assert ep["problem_event_id"] == "r1"
    assert ep["fix_event_id"] == "c2"
    assert ep["verification_event_id"] == "r2"
    assert ep["status"] == "resolved"
    assert ep["confidence"] >= 0.8


def test_extract_episode_unresolved():
    events = [
        _event("c1", EventType.TOOL_CALL, 1, tool_name="Bash"),
        _event("r1", EventType.TOOL_RESULT, 2, content="panic: something awful", error_detected=True),
    ]
    episodes = extract_episodes("sess1", "proj1", events)
    assert len(episodes) == 1
    assert episodes[0]["status"] == "unresolved"
    assert episodes[0]["confidence"] < 0.7


def test_no_episodes_for_clean_session():
    events = [
        _event("u1", EventType.USER_MESSAGE, 1, "Build it"),
        _event("c1", EventType.TOOL_CALL, 2, tool_name="Edit", file_path="/tmp/a.ts", file_operation=FileOperation.EDIT),
        _event("r1", EventType.TOOL_RESULT, 3, content="ok"),
    ]
    episodes = extract_episodes("sess1", "proj1", events)
    assert episodes == []
