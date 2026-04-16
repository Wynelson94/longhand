"""Tests for R2 (context window fidelity) and R3 (output noise cleanup).

Covers:
  - `get_events_by_sequence_range` returns exactly the requested range
    and hides parser `#N` suffix duplicates by default
  - `get_events` and `get_latest_events` hide suffix duplicates too
  - `dedup_suffixes=False` opt-out still works (forensic diagnosis)
  - recall payload omits empty `artifacts`
  - pagination hint names parameters the emitting tool accepts
"""

from __future__ import annotations

import asyncio
import json

from longhand import mcp_server
from longhand.storage.store import LonghandStore


def _insert_events_with_collisions(
    store: LonghandStore,
    session_id: str = "test-sess",
) -> None:
    """Insert synthetic events including some with `#N` suffix event_ids
    to simulate the parser's collision-resolution behavior.
    """
    with store.sqlite.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, transcript_path, started_at, ended_at, "
            "event_count, file_edit_count, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                f"/tmp/{session_id}.jsonl",
                "2026-04-01T00:00:00+00:00",
                "2026-04-01T01:00:00+00:00",
                8,
                0,
                "2026-04-01T02:00:00+00:00",
            ),
        )
        rows = [
            # Primary events at sequences 0..5
            ("evt-0", session_id, "user_message", 0, "2026-04-01T00:00:00+00:00", "A", "{}"),
            ("evt-1", session_id, "assistant_text", 1, "2026-04-01T00:00:01+00:00", "B", "{}"),
            ("evt-2", session_id, "user_message", 2, "2026-04-01T00:00:02+00:00", "C", "{}"),
            ("evt-3", session_id, "assistant_text", 3, "2026-04-01T00:00:03+00:00", "D", "{}"),
            ("evt-4", session_id, "user_message", 4, "2026-04-01T00:00:04+00:00", "E", "{}"),
            ("evt-5", session_id, "assistant_text", 5, "2026-04-01T00:00:05+00:00", "F", "{}"),
            # Collision-resolution duplicates (the parser's #N suffix behavior)
            ("evt-0#1", session_id, "user_message", 0, "2026-04-01T00:00:00+00:00", "A-dup", "{}"),
            ("evt-3#1", session_id, "assistant_text", 3, "2026-04-01T00:00:03+00:00", "D-dup", "{}"),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO events "
            "(event_id, session_id, event_type, sequence, timestamp, content, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


# ─── R2: sequence-range returns exactly the expected window ────────────────


def test_sequence_range_returns_exact_bounds(temp_store: LonghandStore):
    _insert_events_with_collisions(temp_store)

    events = temp_store.sqlite.get_events_by_sequence_range("test-sess", 0, 5)
    assert len(events) == 6, f"expected 6 primary events in 0..5, got {len(events)}"
    assert [e["event_id"] for e in events] == ["evt-0", "evt-1", "evt-2", "evt-3", "evt-4", "evt-5"]
    assert all("#" not in e["event_id"] for e in events)


def test_sequence_range_narrow_bound(temp_store: LonghandStore):
    """seq_start == seq_end should return exactly 1 event."""
    _insert_events_with_collisions(temp_store)
    events = temp_store.sqlite.get_events_by_sequence_range("test-sess", 2, 2)
    assert len(events) == 1
    assert events[0]["event_id"] == "evt-2"


def test_sequence_range_opt_out_shows_suffixes(temp_store: LonghandStore):
    """dedup_suffixes=False surfaces the parser duplicates for forensic work."""
    _insert_events_with_collisions(temp_store)
    events = temp_store.sqlite.get_events_by_sequence_range(
        "test-sess", 0, 5, dedup_suffixes=False
    )
    # 6 primaries + 2 suffix duplicates at sequences 0 and 3
    assert len(events) == 8
    assert "evt-0#1" in {e["event_id"] for e in events}


# ─── R3a: get_events and get_latest_events hide suffixes by default ────────


def test_get_events_hides_suffix_duplicates(temp_store: LonghandStore):
    _insert_events_with_collisions(temp_store)
    events = temp_store.sqlite.get_events(session_id="test-sess")
    ids = [e["event_id"] for e in events]
    assert all("#" not in eid for eid in ids)
    assert len(events) == 6


def test_get_latest_events_hides_suffix_duplicates(temp_store: LonghandStore):
    _insert_events_with_collisions(temp_store)
    events = temp_store.sqlite.get_latest_events("test-sess", limit=10)
    ids = [e["event_id"] for e in events]
    assert all("#" not in eid for eid in ids)
    assert len(events) == 6


# ─── R3b: empty artifacts is omitted from recall payload ──────────────────


def test_recall_omits_empty_artifacts(temp_store: LonghandStore):
    """When recall returns no episodes with linked artifacts, the key is absent."""

    async def _call():
        return await mcp_server._tool_recall(
            temp_store, {"query": "nothing in an empty store", "max_chars": 16000}
        )

    result = asyncio.run(_call())
    payload = json.loads(result[0].text)
    assert "artifacts" not in payload


# ─── R3c: pagination hint matches the emitting tool's parameters ──────────


def test_search_in_context_hint_excludes_offset_and_tail():
    """_HINT_SEARCH_IN_CONTEXT must not suggest params the tool doesn't accept."""
    assert "offset" not in mcp_server._HINT_SEARCH_IN_CONTEXT
    assert "tail" not in mcp_server._HINT_SEARCH_IN_CONTEXT
    assert "limit" in mcp_server._HINT_SEARCH_IN_CONTEXT
    assert "context_events" in mcp_server._HINT_SEARCH_IN_CONTEXT


def test_search_hint_excludes_offset_and_tail():
    assert "offset" not in mcp_server._HINT_SEARCH
    assert "tail" not in mcp_server._HINT_SEARCH


def test_timeline_hint_names_all_its_params():
    """The timeline tool is the only one that actually accepts offset and tail."""
    hint = mcp_server._HINT_TIMELINE
    assert "offset" in hint
    assert "limit" in hint
    assert "tail" in hint
    assert "summary_only" in hint


def test_truncate_output_uses_hint_when_truncated():
    long_text = "x" * 100
    out = mcp_server._truncate_output(long_text, 20, "use smaller limit")
    assert "[... truncated at 20 chars. use smaller limit]" in out


def test_truncate_output_noops_when_under_cap():
    text = "short"
    assert mcp_server._truncate_output(text, 100, "anything") == text


# ─── R2: search_in_context merge is now strict-overlap, not adjacency ─────


def test_search_in_context_merge_is_strict_overlap():
    """Confirm by code inspection that the merge condition dropped `+ 1`.
    Two matches with context_events=2 at sequences 5 and 10 should not merge
    (windows [3,7] and [8,12] — adjacent but not overlapping).
    """
    # This is a guard test: read the source and assert the `+ 1` is gone.
    # Behavioral coverage lives in integration — a full ingest fixture is
    # overkill for something this small.
    import inspect

    src = inspect.getsource(mcp_server._tool_search_in_context)
    # Old bug: `w["seq_start"] <= merged[-1]["seq_end"] + 1`
    assert "seq_end\"] + 1" not in src, (
        "merge condition still uses adjacency (+ 1) — should be strict overlap"
    )
    assert "seq_start\"] <= merged[-1][\"seq_end\"]" in src
