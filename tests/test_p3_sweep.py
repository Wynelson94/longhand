"""P3 sweep (v0.12): MCP store singleton + error envelope, SQL prefix
resolution, redaction card pre-check, oversize reporting, hook-command
spawn targets, and first direct coverage for four previously untested
modules (session_summary_embedding, segment_search, vector_store,
cli/helpers)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from longhand import mcp_server
from longhand.cli.helpers import _format_timestamp, _resolve_prefix
from longhand.redaction import _CARD_PRECHECK
from longhand.types import Event, EventType, Session

# ─── MCP: store singleton + top-level error envelope ─────────────────────────


def test_call_tool_reuses_one_store(monkeypatch, tmp_path):
    built: list[int] = []

    class _FakeStore:
        def __init__(self, *args, **kwargs):
            built.append(1)

    async def _ok(store, arguments):
        return [mcp_server.TextContent(type="text", text="ok")]

    monkeypatch.setattr(mcp_server, "LonghandStore", _FakeStore)
    monkeypatch.setattr(mcp_server, "_STORE", None)
    monkeypatch.setitem(mcp_server._DISPATCH, "fake_tool", _ok)

    asyncio.run(mcp_server.call_tool("fake_tool", {}))
    asyncio.run(mcp_server.call_tool("fake_tool", {}))
    assert built == [1]  # constructed once, reused


def test_call_tool_wraps_handler_errors(monkeypatch):
    async def _boom(store, arguments):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(mcp_server, "_STORE", object())  # skip construction
    monkeypatch.setitem(mcp_server._DISPATCH, "boom_tool", _boom)

    result = asyncio.run(mcp_server.call_tool("boom_tool", {}))
    payload = json.loads(result[0].text)
    assert payload["tool"] == "boom_tool"
    assert "RuntimeError" in payload["error"] and "db exploded" in payload["error"]


# ─── cli/helpers: SQL prefix resolution + timestamp formatting ────────────────


def _seed_session(store, session_id: str, started: str) -> None:
    with store.sqlite.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, project_path, transcript_path,"
            " started_at, ended_at, event_count, user_message_count,"
            " assistant_message_count, tool_call_count, file_edit_count,"
            " ingested_at) VALUES (?, '', '', ?, ?, 0, 0, 0, 0, 0, ?)",
            (session_id, started, started, started),
        )


def test_resolve_prefix_matches_and_prefers_recent(temp_store):
    _seed_session(temp_store, "abc111-old", "2026-01-01T00:00:00Z")
    _seed_session(temp_store, "abc222-new", "2026-06-01T00:00:00Z")
    _seed_session(temp_store, "zzz999", "2026-06-02T00:00:00Z")

    assert _resolve_prefix(temp_store, "zzz") == "zzz999"
    assert _resolve_prefix(temp_store, "abc") == "abc222-new"  # most recent wins
    assert _resolve_prefix(temp_store, "nope") is None


def test_resolve_prefix_escapes_like_wildcards(temp_store):
    _seed_session(temp_store, "a_b-real", "2026-06-01T00:00:00Z")
    _seed_session(temp_store, "axb-decoy", "2026-06-02T00:00:00Z")

    # '_' must match literally, not as the LIKE single-char wildcard —
    # otherwise the newer decoy would win.
    assert _resolve_prefix(temp_store, "a_b") == "a_b-real"


def test_format_timestamp_valid_and_garbage():
    assert _format_timestamp("2026-07-10T18:30:00+00:00") == "2026-07-10 18:30"
    assert _format_timestamp("not-a-date") == "not-a-date"


# ─── redaction: card pre-check ────────────────────────────────────────────────


def test_card_precheck_matches_card_shapes_only():
    assert _CARD_PRECHECK.search("pay with 4111 1111 1111 1111 thanks")
    assert _CARD_PRECHECK.search("4111111111111111")
    assert not _CARD_PRECHECK.search("call 555-1234 or 867-5309")
    assert not _CARD_PRECHECK.search("port 8080, exit code 1, 42 items")


def test_card_still_masked_through_precheck():
    from longhand.redaction import redact_text

    # 4012-8888-8888-1881: Luhn-valid with varied digits — passes the
    # plausibility gate (unlike the classic 4111... test number, which is
    # rejected by design).
    masked, n = redact_text("card: 4012-8888-8888-1881")
    assert n == 1
    assert "4012-8888-8888-1881" not in masked


# ─── reconcile: oversize bucket ───────────────────────────────────────────────


def test_reconcile_reports_oversize_files(temp_store, tmp_path, monkeypatch):
    from longhand.recall import reconcile as reconcile_mod
    from longhand.recall.reconcile import run_reconcile

    big = tmp_path / "huge.jsonl"
    big.write_text('{"type": "user"}\n' * 5)

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [big])
    monkeypatch.setattr(reconcile_mod, "MAX_FILE_SIZE_BYTES", 10)

    report = run_reconcile(temp_store, fix=True)
    assert report.skipped_oversize == [str(big)]
    assert report.missing == []  # not misfiled as re-ingestable
    assert report.ingested == 0
    assert report.to_dict()["skipped_oversize_count"] == 1


# ─── hook install: spawn target must be runnable ──────────────────────────────


def test_hook_install_fallback_never_uses_longhand_cli(monkeypatch, tmp_path):
    """`-m longhand.cli` is a package with no __main__.py — a hook installed
    with that fallback dies on every session end. Same bug class as the
    v0.11.2 background-spawn fix."""
    import longhand.setup_commands as sc

    settings = tmp_path / "settings.json"
    monkeypatch.setattr(sc, "CLAUDE_SETTINGS_PATH", settings)
    monkeypatch.setattr(sc.shutil, "which", lambda name: None)

    sc.hook_install()
    sc.prompt_hook_install()

    text = settings.read_text()
    assert "longhand.cli" not in text
    assert "-m longhand ingest-session" in text
    assert "-m longhand ingest-live" in text
    assert "-m longhand __prompt-hook-run" in text


# ─── coverage: session_summary_embedding ─────────────────────────────────────


def _mini_session() -> Session:
    return Session(
        session_id="s-sum",
        project_path="/Users/tester/proj",
        transcript_path="/Users/tester/t.jsonl",
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        event_count=2,
        user_message_count=1,
        assistant_message_count=1,
        tool_call_count=0,
        file_edit_count=1,
        git_branch="main",
        cwd="/Users/tester/proj",
        model="claude-sonnet-4-6",
    )


def _msg(event_id: str, seq: int, content: str) -> Event:
    return Event(
        event_id=event_id,
        session_id="s-sum",
        event_type=EventType.USER_MESSAGE,
        sequence=seq,
        timestamp=datetime(2026, 7, 1, 0, seq, tzinfo=timezone.utc),
        content=content,
    )


def test_build_session_text_and_metadata():
    from longhand.analysis.session_summary_embedding import (
        build_session_metadata,
        build_session_text,
    )

    session = _mini_session()
    events = [_msg("u1", 1, "Fix the auth middleware timeout")]
    outcome = {"outcome": "fixed", "summary": "Fixed a timeout", "topics": ["auth"]}
    project = {
        "project_id": "p-sum",
        "display_name": "proj",
        "category": "web",
        "keywords": ["auth"],
    }

    text = build_session_text(session, events, outcome, project)
    assert "Project: proj" in text
    assert "Asked: Fix the auth middleware timeout" in text
    assert "Outcome: fixed" in text

    meta = build_session_metadata(session, outcome, project)
    assert meta["session_id"] == "s-sum"
    assert meta["outcome"] == "fixed"


def test_build_project_text():
    from longhand.analysis.session_summary_embedding import build_project_text

    text = build_project_text(
        {"display_name": "proj", "category": "web", "keywords": ["auth"], "aliases": ["proj"]}
    )
    assert "proj" in text and "web" in text


# ─── coverage: segment_search + vector_store round trip ─────────────────────


def test_find_segments_roundtrip(temp_store):
    from longhand.recall.segment_search import find_segments

    temp_store.sqlite.insert_segments(
        [
            {
                "segment_id": "seg-p3",
                "session_id": "s-seg",
                "started_at": "2026-07-01T10:00:00Z",
                "ended_at": "2026-07-01T10:10:00Z",
                "start_sequence": 1,
                "end_sequence": 5,
                "topic": "configuring the resend email domain",
                "summary": "walked through DNS records for the email domain",
            }
        ]
    )
    temp_store.vectors.add_segment_embeddings_batch(
        [
            {
                "segment_id": "seg-p3",
                "text": "configuring the resend email domain DNS records",
                "metadata": {"segment_type": "discussion", "session_id": "s-seg"},
            }
        ]
    )

    hits = find_segments(temp_store, "email domain dns setup")
    assert any(h.get("segment_id") == "seg-p3" for h in hits)


def test_vector_store_episode_roundtrip(temp_store):
    assert temp_store.vectors.episode_count() == 0
    temp_store.vectors.add_episode_embedding(
        episode_id="ep-vs",
        text="database lock error fixed by increasing busy timeout",
        metadata={
            "session_id": "s-vs",
            "project_id": "p",
            "ended_at": "2026-07-01",
            "status": "resolved",
            "has_fix": True,
        },
    )
    assert temp_store.vectors.episode_count() == 1
    hits = temp_store.vectors.search_episodes(query="sqlite lock timeout", n_results=3)
    assert any(h.get("episode_id") == "ep-vs" for h in hits)
