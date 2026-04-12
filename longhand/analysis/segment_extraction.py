"""
Conversation segment extraction.

Clusters contiguous related messages into "segments" — groups of events
about the same topic. Segments are the non-episode retrieval unit: they
capture stories, design discussions, personal conversations, and planning
that don't follow the problem->fix episode pattern.

Deterministic (no LLM). Runs at ingest time alongside episode extraction.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from longhand.types import Event, EventType


# Segment type classification keywords
_DESIGN_KEYWORDS = frozenset({
    "design", "architecture", "structure", "schema", "layout", "component",
    "approach", "pattern", "reasoning", "decision", "strategy", "plan",
    "interface", "api", "endpoint", "model", "database",
})
_PLANNING_KEYWORDS = frozenset({
    "plan", "todo", "roadmap", "next", "milestone", "sprint", "goal",
    "timeline", "schedule", "priority", "backlog", "task",
})
_STORY_MIN_AVG_LENGTH = 120  # lowered from 200 — real stories avg ~150 chars/msg


def _event_type_str(e: Event) -> str:
    """Normalize event type to string."""
    return e.event_type if isinstance(e.event_type, str) else e.event_type.value


def _extract_simple_keywords(text: str) -> set[str]:
    """Extract lowercase tokens 4+ chars for topic-shift detection.

    Lightweight version — NOT the full extract_keywords from topics.py.
    Used only for comparing adjacent user messages within a session.
    """
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", text.lower())
    # Filter common stopwords
    stopwords = {
        "this", "that", "with", "from", "have", "been", "were", "they",
        "will", "would", "could", "should", "about", "there", "their",
        "which", "other", "some", "what", "when", "where", "just",
        "like", "also", "more", "than", "then", "into", "very",
        "want", "need", "know", "think", "make", "going", "does",
    }
    return {t for t in tokens if t not in stopwords}


def _keyword_overlap(kw_a: set[str], kw_b: set[str]) -> float:
    """Jaccard-like overlap ratio between two keyword sets."""
    if not kw_a or not kw_b:
        return 0.0
    intersection = kw_a & kw_b
    union = kw_a | kw_b
    return len(intersection) / len(union) if union else 0.0


def _classify_segment_type(
    events: list[Event],
    user_texts: list[str],
) -> str:
    """Deterministic segment type classification.

    Priority: debugging > design > planning > story > discussion
    """
    # Check for errors -> debugging
    for e in events:
        if getattr(e, "error_detected", False):
            return "debugging"

    # Check for tool calls (indicates work, not just conversation)
    has_tool_calls = any(
        _event_type_str(e) == "tool_call" for e in events
    )

    # Check user message content for design/planning keywords
    combined_text = " ".join(user_texts).lower()
    combined_words = set(re.findall(r"[a-z]{4,}", combined_text))

    if combined_words & _DESIGN_KEYWORDS:
        return "design"
    if combined_words & _PLANNING_KEYWORDS:
        return "planning"

    # Story: long user messages, no tool calls
    if user_texts and not has_tool_calls:
        avg_len = sum(len(t) for t in user_texts) / len(user_texts)
        if avg_len > _STORY_MIN_AVG_LENGTH:
            return "story"

    return "discussion"


def _build_summary(events: list[Event], max_chars: int = 2000) -> str:
    """Concatenate conversational content for embedding.

    Includes user_message, assistant_text, AND assistant_thinking — thinking
    blocks contain design reasoning and diagnosis which are valuable for recall.
    Excludes tool_call and tool_result (noise for topic-level retrieval).
    """
    parts: list[str] = []
    total = 0
    for e in events:
        etype = _event_type_str(e)
        if etype not in ("user_message", "assistant_text", "assistant_thinking"):
            continue
        if not e.content or not e.content.strip():
            continue
        text = e.content.strip()
        remaining = max_chars - total
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining]
        parts.append(text)
        total += len(text)
    return " ".join(parts)


def extract_segments(
    session_id: str,
    project_id: str | None,
    events: list[Event],
) -> list[dict[str, Any]]:
    """Extract conversation segments from a session's events.

    Walks events in sequence order, splitting into segments at:
    - Tool gaps: 6+ tool_call/tool_result events followed by a user_message
    - Topic shifts: < 20% keyword overlap between adjacent user messages
    - Time gaps: > 10 minutes between consecutive events
    - Hard cap: segment exceeds 80 events

    Returns a list of segment dicts ready for SQLite insertion.
    """
    if len(events) < 3:
        return []

    segments: list[dict[str, Any]] = []
    current_start = 0
    prev_user_keywords: set[str] = set()
    tool_run_length = 0  # consecutive tool_call/tool_result count

    def _flush_segment(start_idx: int, end_idx: int) -> None:
        """Build and append a segment from events[start_idx:end_idx+1]."""
        seg_events = events[start_idx : end_idx + 1]
        if len(seg_events) < 3:
            return

        user_texts = [
            e.content.strip()
            for e in seg_events
            if _event_type_str(e) == "user_message" and e.content and e.content.strip()
        ]
        # Allow single user message segments — the event_count >= 3 check
        # already filters truly trivial interactions
        if len(user_texts) < 1:
            return

        # Build segment fields
        topic = user_texts[0][:200] if user_texts else ""
        summary = _build_summary(seg_events)
        if not summary.strip():
            return

        seg_type = _classify_segment_type(seg_events, user_texts)

        # Keywords for the segment (using the simple extractor)
        all_text = " ".join(user_texts)
        keywords = sorted(_extract_simple_keywords(all_text))[:20]

        seg_id = "seg_" + hashlib.sha1(
            f"{session_id}:{seg_events[0].sequence}".encode()
        ).hexdigest()[:16]

        segments.append({
            "segment_id": seg_id,
            "session_id": session_id,
            "project_id": project_id,
            "started_at": seg_events[0].timestamp.isoformat(),
            "ended_at": seg_events[-1].timestamp.isoformat(),
            "start_sequence": seg_events[0].sequence,
            "end_sequence": seg_events[-1].sequence,
            "segment_type": seg_type,
            "topic": topic,
            "summary": summary,
            "event_count": len(seg_events),
            "user_message_count": len(user_texts),
            "keywords": keywords,
        })

    for i, event in enumerate(events):
        etype = _event_type_str(event)

        # Track tool call runs
        if etype in ("tool_call", "tool_result"):
            tool_run_length += 1
        else:
            # Check for boundary conditions on user messages
            if etype == "user_message":
                should_split = False
                segment_length = i - current_start

                # Boundary: tool gap — 6+ tool events then a user message
                # (3 was too aggressive — a single read+result+edit+result is 4 events)
                if tool_run_length >= 6 and segment_length > 3:
                    should_split = True

                # Boundary: topic shift — low keyword overlap
                if event.content and event.content.strip():
                    current_keywords = _extract_simple_keywords(event.content)
                    if prev_user_keywords and current_keywords:
                        overlap = _keyword_overlap(prev_user_keywords, current_keywords)
                        if overlap < 0.2 and segment_length > 3:
                            should_split = True
                    prev_user_keywords = current_keywords

                # Boundary: time gap — > 10 minutes since last event
                # No minimum segment_length guard — a time gap is always a boundary
                if i > 0:
                    try:
                        prev_ts = events[i - 1].timestamp
                        curr_ts = event.timestamp
                        gap_seconds = (curr_ts - prev_ts).total_seconds()
                        if gap_seconds > 600:
                            should_split = True
                    except Exception:
                        pass

                # Boundary: hard cap — segment too long
                # No minimum guard — always split at 80 events
                if segment_length >= 80:
                    should_split = True

                if should_split and i > current_start:
                    _flush_segment(current_start, i - 1)
                    current_start = i

            tool_run_length = 0

    # Flush the final segment
    if current_start < len(events) - 1:
        _flush_segment(current_start, len(events) - 1)

    return segments
