"""Tests for the JSONL parser."""

from __future__ import annotations

from longhand.parser import JSONLParser
from longhand.types import EventType, FileOperation


def test_parse_sample_session(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    # Should have events for each block, not each entry
    assert len(events) > 0

    # Event types
    types = [e.event_type for e in events]
    assert EventType.FILE_SNAPSHOT.value in types
    assert EventType.USER_MESSAGE.value in types
    assert EventType.ASSISTANT_THINKING.value in types
    assert EventType.ASSISTANT_TEXT.value in types
    assert EventType.TOOL_CALL.value in types
    assert EventType.TOOL_RESULT.value in types


def test_multi_block_assistant_splits_into_events(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    # The assistant message with thinking+text+tool_use should produce 3 events
    thinking = [e for e in events if e.event_type == EventType.ASSISTANT_THINKING.value]
    texts = [e for e in events if e.event_type == EventType.ASSISTANT_TEXT.value]
    tools = [e for e in events if e.event_type == EventType.TOOL_CALL.value]

    assert len(thinking) >= 1
    assert len(texts) >= 1
    assert len(tools) >= 2  # Edit + Write
    assert thinking[0].content == "User wants a readme edit."


def test_edit_tool_call_captures_diff(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    edit_call = next(
        (e for e in events if e.event_type == EventType.TOOL_CALL.value and e.tool_name == "Edit"),
        None,
    )
    assert edit_call is not None
    assert edit_call.file_path == "/tmp/test-project/README.md"
    assert edit_call.file_operation == FileOperation.EDIT.value
    assert edit_call.old_content == "Old content"
    assert edit_call.new_content == "New content"


def test_write_tool_call_captures_full_content(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    write_call = next(
        (e for e in events if e.event_type == EventType.TOOL_CALL.value and e.tool_name == "Write"),
        None,
    )
    assert write_call is not None
    assert write_call.file_path == "/tmp/test-project/new.txt"
    assert write_call.file_operation == FileOperation.WRITE.value
    assert write_call.new_content == "Hello, World!"


def test_tool_result_linked_by_tool_use_id(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    results = [e for e in events if e.event_type == EventType.TOOL_RESULT.value]
    assert len(results) == 1
    assert results[0].tool_use_id == "tool-1"
    assert "updated successfully" in results[0].content


def test_session_summary(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    assert session.session_id == "test-session-1"
    assert session.project_path == "/tmp/test-project"
    assert session.git_branch == "main"
    assert session.user_message_count >= 1
    assert session.tool_call_count >= 2
    assert session.file_edit_count >= 2
    assert session.model == "claude-sonnet-4-6"


def test_timestamps_are_timezone_aware(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    for e in events:
        assert e.timestamp.tzinfo is not None


def test_raw_is_preserved(sample_session_file):
    """Every event should preserve the full original JSON entry in raw."""
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    for e in events:
        assert e.raw is not None
        assert isinstance(e.raw, dict)
        # Non-snapshot events should have a type field in the raw entry
        if e.event_type != EventType.FILE_SNAPSHOT.value:
            assert "type" in e.raw
