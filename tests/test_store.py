"""Tests for the storage layer."""

from __future__ import annotations

from longhand.parser import JSONLParser


def test_ingest_and_query_roundtrip(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    result = temp_store.ingest_session(session, events)
    assert result["events_stored"] == len(events)

    # Session retrievable
    stored = temp_store.sqlite.get_session(session.session_id)
    assert stored is not None
    assert stored["session_id"] == "test-session-1"

    # Events retrievable
    stored_events = temp_store.sqlite.get_events(session_id=session.session_id)
    assert len(stored_events) == len(events)


def test_file_edits_filter(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    edits = temp_store.sqlite.get_file_edits("/tmp/test-project/README.md")
    assert len(edits) == 1
    assert edits[0]["tool_name"] == "Edit"


def test_stats(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    stats = temp_store.sqlite.get_stats()
    assert stats["sessions"] == 1
    assert stats["events"] == len(events)
    assert stats["tool_calls"] >= 2
    assert stats["thinking_blocks"] >= 1


def test_already_ingested_detection(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    assert not temp_store.sqlite.already_ingested(
        str(sample_session_file), sample_session_file.stat().st_size
    )

    temp_store.ingest_session(session, events)

    assert temp_store.sqlite.already_ingested(
        str(sample_session_file), sample_session_file.stat().st_size
    )
