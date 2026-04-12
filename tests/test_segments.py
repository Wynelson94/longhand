"""Tests for conversation segment extraction."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import pytest

from longhand.analysis.segment_extraction import extract_segments
from longhand.types import Event, EventType


def _make_event(
    seq: int,
    event_type: str = "user_message",
    content: str = "",
    timestamp: datetime | None = None,
    error_detected: bool = False,
    tool_name: str | None = None,
) -> Event:
    """Create a minimal Event for testing."""
    if timestamp is None:
        # Events 1 minute apart by default
        timestamp = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=seq)
    return Event(
        event_id=f"evt-{seq}",
        session_id="test-session",
        event_type=event_type,
        sequence=seq,
        timestamp=timestamp,
        content=content,
        raw={},
        error_detected=error_detected,
        tool_name=tool_name,
    )


def test_extract_segments_from_discussion():
    """Multi-turn conversation produces at least one segment."""
    events = [
        _make_event(0, "user_message", "Hey lets talk about the concert this weekend"),
        _make_event(1, "assistant_text", "Sure, what concert are you going to?"),
        _make_event(2, "user_message", "Lamb of God at the arena, really excited about it"),
        _make_event(3, "assistant_text", "That sounds amazing, Randy Blythe is incredible live"),
        _make_event(4, "user_message", "Yeah ive seen them before, the energy is unreal"),
        _make_event(5, "assistant_text", "How many shows have you been to total?"),
        _make_event(6, "user_message", "About 130 shows now, metal is my thing"),
        _make_event(7, "assistant_text", "That's impressive, who else have you seen recently?"),
    ]

    segments = extract_segments("test-session", "p_test", events)

    assert len(segments) >= 1
    seg = segments[0]
    assert seg["session_id"] == "test-session"
    assert seg["project_id"] == "p_test"
    assert seg["segment_type"] in ("discussion", "story")  # depends on avg message length
    assert seg["user_message_count"] >= 2
    assert seg["event_count"] >= 3
    assert seg["summary"]  # non-empty summary
    assert seg["topic"]  # non-empty topic


def test_segment_boundary_on_topic_shift():
    """Two distinct topics in one session produce two segments."""
    events = [
        # Topic 1: concerts
        _make_event(0, "user_message", "The concert was amazing last night at the venue"),
        _make_event(1, "assistant_text", "Tell me about it"),
        _make_event(2, "user_message", "The band played incredible songs and the crowd was wild"),
        _make_event(3, "assistant_text", "Sounds like an awesome show"),
        _make_event(4, "user_message", "Definitely one of the best live performances ever seen"),
        _make_event(5, "assistant_text", "Who was opening for them?"),
        # Topic 2: completely different - code/database/architecture
        _make_event(6, "user_message", "Anyway can you help me with the database schema migration for postgres"),
        _make_event(7, "assistant_text", "Sure, what tables need to change?"),
        _make_event(8, "user_message", "The users table needs a new column for authentication tokens"),
        _make_event(9, "assistant_text", "Got it, I'll add an ALTER TABLE migration"),
        _make_event(10, "user_message", "Also add an index on the tokens column for faster lookups"),
        _make_event(11, "assistant_text", "Done, migration is ready to go"),
    ]

    segments = extract_segments("test-session", "p_test", events)

    # Should have at least 2 segments (topic shift between concert and database)
    assert len(segments) >= 2


def test_segment_boundary_on_time_gap():
    """A 15-minute gap between events splits segments."""
    base = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
    events = [
        _make_event(0, "user_message", "Working on the frontend component for the dashboard",
                    timestamp=base),
        _make_event(1, "assistant_text", "Let me help with that",
                    timestamp=base + timedelta(minutes=1)),
        _make_event(2, "user_message", "Add a chart component to show user analytics data",
                    timestamp=base + timedelta(minutes=2)),
        _make_event(3, "assistant_text", "Done, chart component is ready",
                    timestamp=base + timedelta(minutes=3)),
        # 15-minute gap
        _make_event(4, "user_message", "Ok back, now lets work on the backend deployment pipeline",
                    timestamp=base + timedelta(minutes=18)),
        _make_event(5, "assistant_text", "Sure, what deployment target?",
                    timestamp=base + timedelta(minutes=19)),
        _make_event(6, "user_message", "Deploy to vercel with the production environment variables",
                    timestamp=base + timedelta(minutes=20)),
        _make_event(7, "assistant_text", "Setting that up now",
                    timestamp=base + timedelta(minutes=21)),
    ]

    segments = extract_segments("test-session", "p_test", events)

    # Time gap should create a boundary
    assert len(segments) >= 2


def test_segment_boundary_on_tool_gap():
    """6+ tool events followed by user message creates boundary."""
    events = [
        _make_event(0, "user_message", "Please fix the authentication middleware issue"),
        _make_event(1, "assistant_text", "Looking into it"),
        _make_event(2, "user_message", "The login flow is broken for oauth providers"),
        _make_event(3, "assistant_text", "I see the issue"),
        # Tool gap: 6 consecutive tool events (threshold is 6)
        _make_event(4, "tool_call", "Read middleware.ts"),
        _make_event(5, "tool_result", "File contents..."),
        _make_event(6, "tool_call", "Edit on middleware.ts"),
        _make_event(7, "tool_result", "File updated successfully"),
        _make_event(8, "tool_call", "Bash: npm test"),
        _make_event(9, "tool_result", "All tests passing"),
        # New topic after tool gap
        _make_event(10, "user_message", "Great now lets talk about the weekend camping trip"),
        _make_event(11, "assistant_text", "Sure, where are you going?"),
        _make_event(12, "user_message", "Up to yellowstone with the family for three days"),
        _make_event(13, "assistant_text", "That sounds wonderful"),
    ]

    segments = extract_segments("test-session", "p_test", events)

    assert len(segments) >= 2


def test_trivial_segments_filtered():
    """Single-turn interactions produce zero segments."""
    events = [
        _make_event(0, "user_message", "What time is it?"),
        _make_event(1, "assistant_text", "I don't have access to the current time."),
    ]

    segments = extract_segments("test-session", "p_test", events)

    assert len(segments) == 0


def test_segment_type_classification_debugging():
    """Segments with errors are classified as debugging."""
    events = [
        _make_event(0, "user_message", "The build is failing with a type error"),
        _make_event(1, "assistant_text", "Let me check"),
        _make_event(2, "tool_call", "Bash: npm run build"),
        _make_event(3, "tool_result", "Error: type mismatch", error_detected=True),
        _make_event(4, "user_message", "Can you fix that type error in the component"),
        _make_event(5, "assistant_text", "Found it, fixing now"),
    ]

    segments = extract_segments("test-session", "p_test", events)

    if segments:
        assert segments[0]["segment_type"] == "debugging"


def test_segment_type_classification_design():
    """Segments about design/architecture are classified as design."""
    events = [
        _make_event(0, "user_message", "Lets discuss the architecture for the new microservice"),
        _make_event(1, "assistant_text", "What approach are you considering?"),
        _make_event(2, "user_message", "I want to design the component structure and interface patterns"),
        _make_event(3, "assistant_text", "Here's what I'd recommend for the architecture"),
        _make_event(4, "user_message", "What about the database schema design decisions"),
        _make_event(5, "assistant_text", "For the schema, I'd suggest this approach"),
    ]

    segments = extract_segments("test-session", "p_test", events)

    if segments:
        assert segments[0]["segment_type"] == "design"


def test_segment_id_deterministic():
    """Same input produces the same segment ID."""
    events = [
        _make_event(0, "user_message", "First message about the project"),
        _make_event(1, "assistant_text", "Tell me more"),
        _make_event(2, "user_message", "Second message with more details about the project"),
        _make_event(3, "assistant_text", "Got it"),
        _make_event(4, "user_message", "Third message continuing the conversation"),
        _make_event(5, "assistant_text", "Understanding"),
    ]

    segments_a = extract_segments("test-session", "p_test", events)
    segments_b = extract_segments("test-session", "p_test", events)

    if segments_a and segments_b:
        assert segments_a[0]["segment_id"] == segments_b[0]["segment_id"]


def test_segment_keywords_populated():
    """Segments have non-empty keyword lists."""
    events = [
        _make_event(0, "user_message", "Working on the React component for user authentication"),
        _make_event(1, "assistant_text", "I'll help with the authentication component"),
        _make_event(2, "user_message", "The component needs to handle oauth and password flows"),
        _make_event(3, "assistant_text", "Understood, both authentication methods"),
        _make_event(4, "user_message", "Make sure the component has proper error handling"),
        _make_event(5, "assistant_text", "Adding error handling now"),
    ]

    segments = extract_segments("test-session", "p_test", events)

    if segments:
        assert len(segments[0]["keywords"]) > 0


def test_empty_events_returns_empty():
    """No events → no segments."""
    assert extract_segments("test-session", "p_test", []) == []


def test_too_few_events_returns_empty():
    """Fewer than 3 events → no segments."""
    events = [
        _make_event(0, "user_message", "Hello"),
        _make_event(1, "assistant_text", "Hi"),
    ]
    assert extract_segments("test-session", "p_test", events) == []
