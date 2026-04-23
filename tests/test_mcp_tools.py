"""Unit tests for each MCP tool handler (longhand.mcp_server._tool_*).

Each test drives one handler end-to-end against a real SQLite + ChromaDB
fixture store (no mocks). Async handlers are driven through asyncio.run().
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from longhand import mcp_server
from longhand.parser import JSONLParser


def _ingest(fixture_path, store) -> Any:
    parser = JSONLParser(fixture_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events)
    return session


def _call(handler, store, args):
    """Run an async tool handler synchronously."""
    return asyncio.run(handler(store, args))


def _payload(result):
    """Extract and parse the JSON text payload from a tool's TextContent reply."""
    assert len(result) == 1
    text = result[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# ─── Helpers / dispatch wiring ──────────────────────────────────────────────


def test_dispatch_has_all_tools():
    """Every handler is registered and every registration points to a coroutine."""
    assert len(mcp_server._DISPATCH) == 17
    for name, handler in mcp_server._DISPATCH.items():
        assert inspect.iscoroutinefunction(handler), f"{name} is not a coroutine"


# ─── Search tools ───────────────────────────────────────────────────────────


def test_tool_search_returns_hits(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(mcp_server._tool_search, temp_store, {"query": "edit readme", "limit": 5})
    payload = _payload(result)
    # Query doesn't name the project → bare list.
    assert isinstance(payload, list)


def test_tool_search_auto_scopes_on_project_name_match(
    sample_session_file, temp_store
):
    """A query that names a known project should auto-filter to that project's
    events, and the response should advertise the auto-scoping so agents can
    override it."""
    _ingest(sample_session_file, temp_store)
    # sample_session_file's cwd is /tmp/test-project, so the project becomes
    # "test project" after canonicalization.
    result = _call(
        mcp_server._tool_search,
        temp_store,
        {"query": "test project readme", "limit": 5},
    )
    payload = _payload(result)
    assert isinstance(payload, dict), "auto-scoping should wrap response in an object"
    assert payload.get("auto_scoped_to")
    assert "test" in payload["auto_scoped_to"].lower()
    assert "hits" in payload
    assert isinstance(payload["hits"], list)


def test_tool_search_honors_explicit_project_name(
    sample_session_file, temp_store
):
    """Explicit project_name in arguments is never overridden by auto-scoping."""
    _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_search,
        temp_store,
        {
            "query": "readme",
            "project_name": "test project",
            "limit": 5,
        },
    )
    payload = _payload(result)
    # Explicit filter → bare list, no auto_scoped_to annotation.
    assert isinstance(payload, list)


def test_tool_search_in_context_unknown_session(temp_store):
    result = _call(
        mcp_server._tool_search_in_context,
        temp_store,
        {"session_id": "nonexistent", "query": "anything"},
    )
    assert "No session matching prefix" in result[0].text


def test_tool_search_in_context_finds_match(sample_session_file, temp_store):
    session = _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_search_in_context,
        temp_store,
        {"session_id": session.session_id[:8], "query": "readme"},
    )
    text = result[0].text
    # Either a hit payload with context_windows, or a helpful fallback string
    assert "context_windows" in text or "No matches" in text or "no sequence" in text


# ─── Session-level reads ────────────────────────────────────────────────────


def test_tool_list_sessions(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(mcp_server._tool_list_sessions, temp_store, {"limit": 10})
    payload = _payload(result)
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_tool_get_session_timeline(sample_session_file, temp_store):
    session = _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_get_session_timeline,
        temp_store,
        {"session_id": session.session_id, "limit": 50},
    )
    payload = _payload(result)
    assert "meta" in payload
    assert "events" in payload
    assert payload["meta"]["session_id"] == session.session_id


def test_tool_get_session_timeline_unknown_session(temp_store):
    result = _call(
        mcp_server._tool_get_session_timeline,
        temp_store,
        {"session_id": "no-such-session"},
    )
    assert "No session matching" in result[0].text


def test_tool_get_session_timeline_summary_only(sample_session_file, temp_store):
    session = _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_get_session_timeline,
        temp_store,
        {"session_id": session.session_id, "summary_only": True, "limit": 100},
    )
    payload = _payload(result)
    for e in payload["events"]:
        assert "content" not in e


def test_tool_get_latest_events(sample_session_file, temp_store):
    session = _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_get_latest_events,
        temp_store,
        {"session_id": session.session_id, "limit": 5},
    )
    payload = _payload(result)
    assert payload["meta"]["order"] == "sequence DESC"
    assert len(payload["events"]) <= 5


# ─── File replay ────────────────────────────────────────────────────────────


def test_tool_replay_file(multi_edit_session_file, temp_store):
    session = _ingest(multi_edit_session_file, temp_store)
    result = _call(
        mcp_server._tool_replay_file,
        temp_store,
        {"session_id": session.session_id, "file_path": "/tmp/test/sample.py"},
    )
    payload = _payload(result)
    assert payload["file_path"] == "/tmp/test/sample.py"
    assert "content" in payload


def test_tool_replay_file_unknown_file(sample_session_file, temp_store):
    session = _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_replay_file,
        temp_store,
        {"session_id": session.session_id, "file_path": "/never/edited.txt"},
    )
    assert "No edits found" in result[0].text


def test_tool_get_file_history(multi_edit_session_file, temp_store):
    _ingest(multi_edit_session_file, temp_store)
    result = _call(
        mcp_server._tool_get_file_history,
        temp_store,
        {"file_path": "/tmp/test/sample.py"},
    )
    payload = _payload(result)
    assert isinstance(payload, list)
    assert len(payload) >= 3, "multi-edit fixture writes once and edits 3x"


# ─── Stats ──────────────────────────────────────────────────────────────────


def test_tool_get_stats(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(mcp_server._tool_get_stats, temp_store, {})
    payload = _payload(result)
    assert "sessions" in payload or "total_events" in payload or isinstance(payload, dict)


# ─── Proactive memory tools ─────────────────────────────────────────────────


def test_tool_recall(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_recall,
        temp_store,
        {"query": "readme edit", "max_episodes": 3},
    )
    payload = _payload(result)
    assert "query" in payload
    assert "project_matches" in payload


def test_tool_recall_project_status_unknown(temp_store):
    result = _call(
        mcp_server._tool_recall_project_status,
        temp_store,
        {"project": "nonexistent-project-xyz"},
    )
    assert "No project matched" in result[0].text


def test_tool_recall_project_status_exposes_staleness(
    sample_session_file, temp_store, tmp_path, monkeypatch
):
    """recall_project_status must surface disk↔DB drift.

    When a JSONL exists on disk referencing the project but isn't in the
    sessions table, `stale=True` with a reason string pointing at
    `longhand reconcile --fix`.
    """
    import shutil

    # Ingest one session so a project entity exists for "test-project".
    _ingest(sample_session_file, temp_store)

    # Copy the JSONL to a second path so on-disk count > indexed count.
    second_copy = tmp_path / "second-session.jsonl"
    shutil.copy(sample_session_file, second_copy)

    # Point discover_sessions at both files — the second is on disk but NOT
    # in the sessions table.
    from longhand.recall import recall_pipeline

    monkeypatch.setattr(
        recall_pipeline,
        "discover_sessions",
        lambda *a, **kw: [sample_session_file, second_copy],
    )

    result = _call(
        mcp_server._tool_recall_project_status,
        temp_store,
        {"project": "test-project"},
    )
    payload = _payload(result)

    assert payload["session_count_indexed"] == 1
    assert payload["session_count_on_disk"] == 2
    assert payload["stale"] is True
    assert payload["stale_reason"]
    assert "reconcile" in payload["stale_reason"]
    # Narrative should include the drift warning up front.
    assert "⚠" in payload["narrative"] or "reconcile" in payload["narrative"]


def test_tool_recall_project_status_not_stale_when_in_sync(
    sample_session_file, temp_store, monkeypatch
):
    """When every on-disk JSONL for the project is ingested, stale=False."""
    from longhand.recall import recall_pipeline

    _ingest(sample_session_file, temp_store)

    monkeypatch.setattr(
        recall_pipeline,
        "discover_sessions",
        lambda *a, **kw: [sample_session_file],
    )

    result = _call(
        mcp_server._tool_recall_project_status,
        temp_store,
        {"project": "test-project"},
    )
    payload = _payload(result)

    assert payload["session_count_indexed"] == 1
    assert payload["session_count_on_disk"] == 1
    assert payload["stale"] is False
    assert payload["stale_reason"] is None


def test_narrative_drops_commits_with_blank_hash():
    """Historical git_operations rows with NULL/empty commit_hash should not
    produce blank backticks in the narrative."""
    from longhand.recall.narrative import build_project_status_narrative

    last_commits = [
        {"commit_hash": "abc1234", "commit_message": "fix thing", "timestamp": "2026-04-23T00:00:00+00:00"},
        {"commit_hash": None, "commit_message": None, "timestamp": "2026-04-23T00:00:00+00:00"},
        {"commit_hash": "", "commit_message": "ghost", "timestamp": "2026-04-23T00:00:00+00:00"},
    ]
    narrative = build_project_status_narrative(
        display_name="foo",
        canonical_path="/tmp/foo",
        last_commits=last_commits,
        active_branch=None,
        recent_sessions=[{"session_id": "s1", "started_at": "2026-04-23T00:00:00+00:00", "event_count": 1}],
        recent_episodes=[],
        unresolved_episodes=[],
        recent_segments=[],
        last_outcome={"outcome": "fixed"},
    )
    # The good commit renders; the bad ones don't.
    assert "abc1234" in narrative
    assert "`` " not in narrative  # no empty backtick-pair
    assert "ghost" not in narrative
    # Count uses filtered list
    assert "Recent commits (1)" in narrative


def test_narrative_uses_episode_fix_summary_not_user_message():
    """The last-session outcome trailer should come from an episode's
    fix_summary, not session_outcomes.summary (which is a user message)."""
    from longhand.recall.narrative import build_project_status_narrative

    narrative = build_project_status_narrative(
        display_name="foo",
        canonical_path="/tmp/foo",
        last_commits=[],
        active_branch=None,
        recent_sessions=[{"session_id": "s1", "started_at": "2026-04-23T00:00:00+00:00", "event_count": 1}],
        recent_episodes=[{"fix_summary": "refactored the auth middleware to scope tokens"}],
        unresolved_episodes=[],
        recent_segments=[],
        last_outcome={
            "outcome": "fixed",
            "summary": "fixed: can you pull my bsoi-ops from my git and review",
        },
        latest_fix_summary="refactored the auth middleware to scope tokens",
    )
    assert "refactored the auth middleware" in narrative
    assert "Last fix: refactored the auth middleware" in narrative
    # The old polluted summary must not leak in.
    assert "pull my bsoi-ops from my git" not in narrative


def test_narrative_outcome_only_when_no_episode_summary():
    """No episode fix_summary → outcome word stands alone, no misleading text."""
    from longhand.recall.narrative import build_project_status_narrative

    narrative = build_project_status_narrative(
        display_name="foo",
        canonical_path="/tmp/foo",
        last_commits=[],
        active_branch=None,
        recent_sessions=[{"session_id": "s1", "started_at": "2026-04-23T00:00:00+00:00", "event_count": 1}],
        recent_episodes=[],
        unresolved_episodes=[],
        recent_segments=[],
        last_outcome={
            "outcome": "fixed",
            "summary": "fixed: can you pull my bsoi-ops from my git",
        },
    )
    assert "**fixed**" in narrative
    assert "Last fix:" not in narrative
    assert "pull my bsoi-ops" not in narrative


def test_tool_match_project(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(mcp_server._tool_match_project, temp_store, {"query": "project", "top_k": 3})
    payload = _payload(result)
    assert isinstance(payload, list)


# ─── Episodes ───────────────────────────────────────────────────────────────


def test_tool_find_episodes(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_find_episodes,
        temp_store,
        {"limit": 10, "has_fix": False},
    )
    payload = _payload(result)
    assert isinstance(payload, list)


def test_tool_get_episode_unknown(temp_store):
    result = _call(
        mcp_server._tool_get_episode,
        temp_store,
        {"episode_id": "no-such-episode"},
    )
    assert "No episode" in result[0].text


# ─── Git / commits ──────────────────────────────────────────────────────────


def test_tool_get_session_commits(sample_session_file, temp_store):
    session = _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_get_session_commits,
        temp_store,
        {"session_id": session.session_id},
    )
    payload = _payload(result)
    assert isinstance(payload, list)


def test_tool_get_session_commits_unknown(temp_store):
    result = _call(
        mcp_server._tool_get_session_commits,
        temp_store,
        {"session_id": "nope"},
    )
    assert "No session matching" in result[0].text


def test_tool_find_commits(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(mcp_server._tool_find_commits, temp_store, {"query": "commit", "limit": 5})
    payload = _payload(result)
    assert isinstance(payload, list)


# ─── Projects ───────────────────────────────────────────────────────────────


def test_tool_list_projects(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(mcp_server._tool_list_projects, temp_store, {"limit": 10})
    payload = _payload(result)
    assert isinstance(payload, list)


def test_tool_list_projects_verbose(sample_session_file, temp_store):
    _ingest(sample_session_file, temp_store)
    result = _call(
        mcp_server._tool_list_projects,
        temp_store,
        {"limit": 10, "verbose": True},
    )
    payload = _payload(result)
    assert isinstance(payload, list)


def test_tool_get_project_timeline_empty(temp_store):
    result = _call(
        mcp_server._tool_get_project_timeline,
        temp_store,
        {"project_id": "no-such-project"},
    )
    payload = _payload(result)
    assert payload == []


# ─── End-to-end dispatch ────────────────────────────────────────────────────


def test_dispatch_unknown_tool(temp_store):
    """call_tool routes unknown names through the fallback branch."""
    # Directly exercise the dispatch via a helper handler — the real call_tool
    # instantiates its own store, so we test the routing through _DISPATCH.
    assert mcp_server._DISPATCH.get("not-a-real-tool") is None


# ensure pytest doesn't collect the Any import as a test
_ = pytest, Any
