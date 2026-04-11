"""Tests for search_in_context MCP tool and supporting storage method."""

from __future__ import annotations

from longhand.parser import JSONLParser


def test_get_events_by_sequence_range(sample_session_file, temp_store):
    """The new SQLite method returns events within a sequence range."""
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    all_events = temp_store.sqlite.get_events(session_id=session.session_id)
    assert len(all_events) >= 3, "Need at least 3 events for a meaningful range test"

    # Get a subset by sequence range
    seqs = [e["sequence"] for e in all_events]
    mid = seqs[len(seqs) // 2]
    result = temp_store.sqlite.get_events_by_sequence_range(
        session.session_id, mid - 1, mid + 1
    )
    # Should return at least 1 event, at most 3
    assert len(result) >= 1
    assert len(result) <= 3
    # All returned events should be within the range
    for r in result:
        assert mid - 1 <= r["sequence"] <= mid + 1
    # Should be ordered by sequence
    result_seqs = [r["sequence"] for r in result]
    assert result_seqs == sorted(result_seqs)


def test_get_events_by_sequence_range_empty(sample_session_file, temp_store):
    """Returns empty list for a range with no events."""
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    result = temp_store.sqlite.get_events_by_sequence_range(
        session.session_id, 99999, 99999
    )
    assert result == []


def test_get_events_by_sequence_range_wrong_session(sample_session_file, temp_store):
    """Returns empty list for a non-existent session."""
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    result = temp_store.sqlite.get_events_by_sequence_range(
        "nonexistent-session", 0, 100
    )
    assert result == []


def test_get_events_by_sequence_range_full_span(sample_session_file, temp_store):
    """A wide range returns all events in the session."""
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    all_events = temp_store.sqlite.get_events(session_id=session.session_id)
    result = temp_store.sqlite.get_events_by_sequence_range(
        session.session_id, 0, 99999
    )
    assert len(result) == len(all_events)
