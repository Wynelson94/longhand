"""
Session outcome classification.

Rules-based. No LLM. Classifies a session as:
- shipped: many edits + git activity + no errors → productive forward progress
- fixed: had errors, then ended clean → debug session that resolved
- stuck: had errors, didn't resolve → unresolved problem
- abandoned: short session with no conclusive activity
- exploratory: mostly reads, few or no edits → investigation
"""

from __future__ import annotations

from typing import Any

from longhand.extractors.topics import extract_keywords
from longhand.types import Event, Session


def classify_session(session: Session, events: list[Event]) -> dict[str, Any]:
    """Classify the outcome of a session based on its events."""

    error_events = []
    fix_candidates = 0
    test_pass = 0
    test_fail = 0
    edit_count = 0
    read_count = 0
    bash_success = 0
    bash_failure = 0

    first_error_event_id: str | None = None
    last_error_idx: int | None = None
    last_success_after_error_idx: int | None = None

    user_texts: list[str] = []
    thinking_texts: list[str] = []

    for idx, e in enumerate(events):
        etype = e.event_type if isinstance(e.event_type, str) else e.event_type.value

        if etype == "user_message":
            user_texts.append(e.content or "")

        if etype == "assistant_thinking":
            thinking_texts.append(e.content or "")

        if etype == "tool_call":
            fop = e.file_operation if isinstance(e.file_operation, str) or e.file_operation is None else e.file_operation.value
            if fop in ("edit", "write", "multi_edit", "notebook_edit"):
                edit_count += 1
                fix_candidates += 1
            elif fop == "read":
                read_count += 1

        if etype == "tool_result":
            # Error detection from parse-time fields
            if e.error_detected:
                error_events.append(e)
                if first_error_event_id is None:
                    first_error_event_id = e.event_id
                last_error_idx = idx

                # Test-specific error
                if e.error_category == "test":
                    test_fail += 1

            else:
                # Clean result after an error?
                if last_error_idx is not None and idx > last_error_idx:
                    last_success_after_error_idx = idx

                # Count test passes from bash outputs
                if e.tool_name is None and e.content:
                    if any(k in e.content.lower() for k in ["passed", "✓", "ok", "success"]):
                        if "test" in e.content.lower():
                            test_pass += 1

                # Bash success tracking
                if e.tool_success:
                    bash_success += 1

    # Classification
    outcome: str
    confidence: float

    has_errors = len(error_events) > 0
    ended_clean = has_errors and last_success_after_error_idx is not None
    edit_heavy = edit_count >= 3
    read_heavy = read_count > edit_count * 2 and edit_count < 3

    if has_errors and ended_clean:
        outcome = "fixed"
        confidence = 0.7 + min(0.3, (last_success_after_error_idx or 0) / max(len(events), 1) * 0.3)
    elif has_errors and not ended_clean:
        outcome = "stuck"
        confidence = 0.6
    elif edit_heavy and not has_errors:
        outcome = "shipped"
        confidence = 0.75
    elif read_heavy:
        outcome = "exploratory"
        confidence = 0.8
    elif len(events) < 5:
        outcome = "abandoned"
        confidence = 0.5
    else:
        outcome = "shipped" if edit_count > 0 else "exploratory"
        confidence = 0.55

    # Topic extraction for proactive search later
    topics = extract_keywords(user_texts + thinking_texts, top_k=10, min_count=1)

    # One-paragraph deterministic summary
    first_user = user_texts[0][:200] if user_texts else ""
    summary = f"{outcome}: {first_user}".strip()
    if len(summary) > 500:
        summary = summary[:500] + "..."

    return {
        "session_id": session.session_id,
        "outcome": outcome,
        "confidence": confidence,
        "error_count": len(error_events),
        "fix_count": fix_candidates,
        "test_pass_count": test_pass,
        "test_fail_count": test_fail,
        "first_error_event_id": first_error_event_id,
        "resolution_event_id": (
            events[last_success_after_error_idx].event_id
            if last_success_after_error_idx is not None
            else None
        ),
        "summary": summary,
        "topics": topics,
    }
