"""Tests for get_latest_events SQLite method and MCP tool."""

from __future__ import annotations

from longhand.parser import JSONLParser


def _ingest(fixture_path, store):
    parser = JSONLParser(fixture_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events)
    return session


def test_get_latest_events_default_desc_order(sample_session_file, temp_store):
    """Returns events in reverse chronological order (sequence DESC)."""
    session = _ingest(sample_session_file, temp_store)

    result = temp_store.sqlite.get_latest_events(session.session_id, limit=100)
    assert len(result) > 0

    seqs = [e["sequence"] for e in result]
    assert seqs == sorted(seqs, reverse=True), "expected DESC order by sequence"


def test_get_latest_events_respects_limit(sample_session_file, temp_store):
    """Returns at most `limit` events, taking the most recent ones."""
    session = _ingest(sample_session_file, temp_store)

    all_events = temp_store.sqlite.get_events(session_id=session.session_id)
    assert len(all_events) >= 3, "sample fixture must have ≥3 events"

    top2 = temp_store.sqlite.get_latest_events(session.session_id, limit=2)
    assert len(top2) == 2

    # Top 2 should be the two highest-sequence events
    expected_top_seqs = sorted(
        (e["sequence"] for e in all_events), reverse=True
    )[:2]
    actual_top_seqs = [e["sequence"] for e in top2]
    assert actual_top_seqs == expected_top_seqs


def test_get_latest_events_event_type_filter(sample_session_file, temp_store):
    """event_type filter narrows results to one type, still in DESC order."""
    session = _ingest(sample_session_file, temp_store)

    user_msgs = temp_store.sqlite.get_latest_events(
        session.session_id, limit=10, event_type="user_message"
    )
    assert len(user_msgs) > 0
    for e in user_msgs:
        assert e["event_type"] == "user_message"

    seqs = [e["sequence"] for e in user_msgs]
    assert seqs == sorted(seqs, reverse=True)


def test_get_latest_events_unknown_session(temp_store):
    """Unknown session_id returns an empty list (no error)."""
    result = temp_store.sqlite.get_latest_events("no-such-session", limit=10)
    assert result == []


def test_get_latest_events_limit_larger_than_events(sample_session_file, temp_store):
    """limit larger than total event count returns all events, no error."""
    session = _ingest(sample_session_file, temp_store)

    all_events = temp_store.sqlite.get_events(session_id=session.session_id)
    result = temp_store.sqlite.get_latest_events(session.session_id, limit=100000)
    assert len(result) == len(all_events)
