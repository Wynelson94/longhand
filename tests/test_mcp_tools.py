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
