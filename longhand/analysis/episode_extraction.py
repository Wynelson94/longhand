"""
Episode extraction — the hardest piece of proactive memory.

An "episode" is a problem→fix sequence within a session:
  error_event → assistant_thinking (diagnosis) → tool_call (fix) → verification

Deterministic forward walk. No LLM. Confidence scores let the recall layer
filter low-quality matches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from longhand.extractors.file_refs import extract_file_references
from longhand.types import Event

# Events we consider "edits" (candidate fixes)
_FIX_OPERATIONS = {"edit", "write", "multi_edit", "notebook_edit"}

# How many events to look back from a problem event for the anchoring user
# message. Long Claude runs can emit hundreds of tool calls between user
# turns, so the lookback needs to be generous. Capped to avoid scanning
# the whole session for episodes triggered deep in multi-hour sessions.
_USER_LOOKBACK_EVENTS = 100

# How many chars of rich summary text to preserve before truncation.
_PROBLEM_DESCRIPTION_MAX = 600
_DIAGNOSIS_SUMMARY_MAX = 800
_FIX_SUMMARY_MAX = 600

# Minimum substantive length for assistant_text snippets to include in
# summary concatenation (filters out one-word acknowledgements).
_MIN_TEXT_SNIPPET = 40


def _get_op(e: Event) -> str | None:
    if e.file_operation is None:
        return None
    return e.file_operation if isinstance(e.file_operation, str) else e.file_operation.value


def _get_type(e: Event) -> str:
    return e.event_type if isinstance(e.event_type, str) else e.event_type.value


def _episode_id(session_id: str, start_idx: int) -> str:
    return "ep_" + hashlib.sha256(f"{session_id}:{start_idx}".encode()).hexdigest()[:16]


def extract_episodes(
    session_id: str,
    project_id: str | None,
    events: list[Event],
) -> list[dict[str, Any]]:
    """Walk events forward and extract problem→fix episodes.

    Algorithm:
    1. See a tool_result with error_detected → open a candidate episode.
    2. The problem description = previous user_message OR the error snippet itself.
    3. Scan forward for:
       - assistant_thinking that references the error or touched files → diagnosis
       - tool_call (edit/write) that touches a file named in the error → fix
       - subsequent tool_result that's clean → verification
    4. Close episode at verification, at the next independent error, or at session end.
    """
    episodes: list[dict[str, Any]] = []
    i = 0

    while i < len(events):
        e = events[i]
        if _get_type(e) != "tool_result" or not e.error_detected:
            i += 1
            continue

        # Problem identification
        problem_event = e
        problem_files = extract_file_references(e.content or "")

        # Walk backward for the most recent user_message (the request). The
        # lookback is deliberately generous (_USER_LOOKBACK_EVENTS = 100)
        # because long Claude runs can emit dozens of tool calls between
        # user turns — an error mid-run should still anchor to the original
        # ask, not just the raw error text.
        preceding_user = None
        lookback_start = max(i - _USER_LOOKBACK_EVENTS, -1)
        for j in range(i - 1, lookback_start, -1):
            if _get_type(events[j]) == "user_message" and (events[j].content or "").strip():
                preceding_user = events[j]
                break

        # Compose problem_description from BOTH user ask and error signal.
        # Mixing them gives the embedding both "intent vocabulary" (what
        # the user was trying to do) and "surface vocabulary" (what broke).
        problem_description = _compose_problem_description(
            preceding_user,
            problem_event,
            max_chars=_PROBLEM_DESCRIPTION_MAX,
        )

        # Forward scan — collect structural events (diagnosis, fix,
        # verification) AND accumulate substantive text/thinking events
        # spanning the problem→fix window for the rich diagnosis summary.
        diagnosis_event = None
        fix_event = None
        verification_event = None
        diagnosis_texts: list[str] = []  # thinking + assistant_text between problem and fix
        fix_intent_text: str | None = None  # assistant_text immediately preceding fix
        touched_files: set[str] = set(problem_files)
        tags: set[str] = set()
        if e.error_category:
            tags.add(e.error_category)

        j = i + 1
        last_substantive_text: Event | None = None  # running pointer for fix intent
        while j < len(events):
            cand = events[j]
            ctype = _get_type(cand)

            # Another independent error → close current episode
            if ctype == "tool_result" and cand.error_detected and j > i + 1:
                # Only treat as independent if we haven't found a fix yet
                # OR if the error is in a different file
                new_files = set(extract_file_references(cand.content or ""))
                if not fix_event or (new_files and new_files.isdisjoint(touched_files)):
                    break

            # Accumulate diagnosis material — any thinking or substantive
            # assistant_text between problem and fix. Collected BEFORE the
            # diagnosis_event capture so we include all reasoning, not just
            # the first keyword-matching thinking block.
            if fix_event is None and ctype in ("assistant_thinking", "assistant_text"):
                text = (cand.content or "").strip()
                if text and len(text) >= _MIN_TEXT_SNIPPET:
                    diagnosis_texts.append(text)
                    if ctype == "assistant_text":
                        last_substantive_text = cand

            # Diagnosis: first thinking block that mentions the error or
            # referenced files — kept for the structural fk link
            if ctype == "assistant_thinking" and diagnosis_event is None:
                content = (cand.content or "").lower()
                if e.error_snippet:
                    error_keywords = _extract_keywords(e.error_snippet)[:5]
                    if any(word in content for word in error_keywords):
                        diagnosis_event = cand
                if diagnosis_event is None and problem_files:
                    if any(Path(f).name.lower() in content for f in problem_files):
                        diagnosis_event = cand

            # Fix: tool_call that edits a file plausibly related to the bug.
            # Fix-capture predicate — three independent signals, any suffices:
            #   1. file-in-error: the error text literally named this file
            #   2. diagnosis-link: a keyword-matching thinking block fired
            #   3. file-in-reasoning: Claude's accumulated diagnosis_texts
            #      reference this file's basename (Claude thought about it
            #      in paraphrase, then edited it — real-world common case
            #      that signal #1 and #2 miss because they require exact
            #      vocabulary overlap with the error text).
            if ctype == "tool_call" and _get_op(cand) in _FIX_OPERATIONS:
                if cand.file_path:
                    touched_files.add(cand.file_path)
                if fix_event is None:
                    # Match either "middleware.py" (exact) or "middleware"
                    # (stem) — Claude usually refers to files by stem in
                    # prose (e.g. "the auth middleware") but may use the
                    # full basename when quoting paths.
                    fname_full = (
                        Path(cand.file_path).name.lower()
                        if cand.file_path else ""
                    )
                    fname_stem = (
                        Path(cand.file_path).stem.lower()
                        if cand.file_path else ""
                    )
                    error_file_match = bool(
                        problem_files and cand.file_path and any(
                            Path(ref).name == Path(cand.file_path).name
                            for ref in problem_files
                        )
                    )
                    reasoning_file_match = bool(
                        (fname_full or fname_stem) and diagnosis_texts and any(
                            (fname_full and fname_full in text.lower())
                            or (fname_stem and len(fname_stem) >= 4
                                and fname_stem in text.lower())
                            for text in diagnosis_texts
                        )
                    )
                    if (
                        error_file_match
                        or diagnosis_event is not None
                        or reasoning_file_match
                    ):
                        fix_event = cand
                        # Capture "intent" — the most recent substantive
                        # assistant_text before this fix. This is usually
                        # Claude saying "let me change X by doing Y"
                        # which is exactly the semantic signal we want.
                        if last_substantive_text is not None:
                            intent_text = (last_substantive_text.content or "").strip()
                            if intent_text:
                                fix_intent_text = intent_text

            # Verification: a clean tool_result after a fix
            if ctype == "tool_result" and not cand.error_detected and fix_event is not None:
                if cand.tool_success is not False:
                    verification_event = cand
                    j += 1
                    break

            j += 1

        # Confidence scoring
        confidence = 0.3
        if diagnosis_event:
            confidence += 0.2
        if fix_event:
            confidence += 0.3
        if verification_event:
            confidence += 0.2
        confidence = min(1.0, confidence)

        status = "resolved" if verification_event else ("partial" if fix_event else "unresolved")

        # ── Rich summaries (all verbatim concatenation — no LLM) ──────────

        # Diagnosis summary: all thinking + substantive text between the
        # problem and the fix (or, if no fix, everything in the forward
        # scan range). Captures Claude's mental model of the bug.
        diagnosis_summary = _compose_diagnosis_summary(
            diagnosis_texts, max_chars=_DIAGNOSIS_SUMMARY_MAX
        )

        # Fix summary: preceding intent text (if any) + the mechanical diff
        # + verification signal (if any). Gives an intent→action→outcome
        # arc that a semantic embedding can latch onto.
        fix_summary = _compose_fix_summary(
            fix_event,
            fix_intent_text,
            verification_event,
            max_chars=_FIX_SUMMARY_MAX,
        )

        ep_id = _episode_id(session_id, i)

        episodes.append({
            "episode_id": ep_id,
            "session_id": session_id,
            "project_id": project_id,
            "started_at": problem_event.timestamp.isoformat(),
            "ended_at": (verification_event or fix_event or problem_event).timestamp.isoformat(),
            "problem_event_id": problem_event.event_id,
            "diagnosis_event_id": diagnosis_event.event_id if diagnosis_event else None,
            "fix_event_id": fix_event.event_id if fix_event else None,
            "verification_event_id": verification_event.event_id if verification_event else None,
            "problem_description": problem_description,
            "diagnosis_summary": diagnosis_summary,
            "fix_summary": fix_summary,
            "touched_files": sorted(touched_files),
            "tags": sorted(tags),
            "confidence": confidence,
            "status": status,
        })

        # Move past this episode — start scanning from after the verification
        # (or from the next event if we didn't find one)
        i = max(j, i + 1)

    # Link episodes to git commits: look forward from each fix for a commit
    _link_commits_to_episodes(episodes, events)

    return episodes


def _link_commits_to_episodes(episodes: list[dict], events: list[Event]) -> None:
    """Post-pass: for each episode with a fix, find the next git commit and link it."""
    # Build index: event_id → sequence position
    event_seq: dict[str, int] = {e.event_id: idx for idx, e in enumerate(events)}

    # Collect commit events
    commit_events = [
        (idx, e) for idx, e in enumerate(events)
        if e.git_operation == "commit" and e.git_commit_hash
    ]

    for ep in episodes:
        fix_eid = ep.get("fix_event_id")
        if not fix_eid or fix_eid not in event_seq:
            continue

        fix_idx = event_seq[fix_eid]
        # Look for the next commit within 30 events of the fix
        for commit_idx, commit_event in commit_events:
            if commit_idx > fix_idx and (commit_idx - fix_idx) <= 30:
                ep["fix_commit_hash"] = commit_event.git_commit_hash
                break


def _extract_keywords(text: str) -> list[str]:
    """Tiny keyword extractor for matching diagnosis thinking to error snippets."""
    import re
    tokens = re.findall(r"\w{4,}", text.lower())
    # Deduplicate, keep order
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ─── Summary composition helpers ────────────────────────────────────────────
#
# All verbatim concatenation of existing event content. No LLM calls,
# no novel summarization — the principle is "avoid AI summary memory and
# remain forensic." Every character emitted came from a real event in
# the session.


def _compose_problem_description(
    preceding_user: Event | None,
    problem_event: Event,
    max_chars: int,
) -> str:
    """Combine the user's ask (if found) with the error signal into a single
    semantically rich description.

    Format: "Ask: <user request truncated>. Error: <error content truncated>"

    Either piece can be missing — degrade to the other. Labels are kept
    explicit so the embedding carries structural cues.
    """
    parts: list[str] = []

    if preceding_user and preceding_user.content:
        ask = preceding_user.content.strip()
        if ask:
            # Give the user ask ~60% of the budget
            ask_budget = int(max_chars * 0.6)
            parts.append(f"Ask: {ask[:ask_budget]}")

    # Always include an error signal — prefer the dedicated error_snippet
    # field over the raw content (shorter, more focused).
    error_text = (problem_event.error_snippet or problem_event.content or "").strip()
    if error_text:
        remaining = max_chars - sum(len(p) + 2 for p in parts)
        if remaining > 40:  # only include if there's meaningful space
            parts.append(f"Error: {error_text[:remaining]}")

    return ". ".join(parts) if parts else ""


def _compose_diagnosis_summary(
    diagnosis_texts: list[str],
    max_chars: int,
) -> str:
    """Concatenate thinking + assistant_text captured between problem and fix.

    Order preserved (earliest first). Separator `" | "` delimits segments
    so the embedding still has structure. Truncated to max_chars with
    priority on earliest reasoning (often contains the core hypothesis).
    """
    if not diagnosis_texts:
        return ""

    out_parts: list[str] = []
    used = 0
    for text in diagnosis_texts:
        if used >= max_chars:
            break
        remaining = max_chars - used
        snippet = text[:remaining].strip()
        if snippet:
            out_parts.append(snippet)
            used += len(snippet) + 3  # +3 for " | " separator

    return " | ".join(out_parts)


def _compose_fix_summary(
    fix_event: Event | None,
    intent_text: str | None,
    verification_event: Event | None,
    max_chars: int,
) -> str:
    """Compose the fix summary: intent + mechanical diff + verification.

    - Intent: the most recent substantive assistant_text before the fix
      (what Claude said it was about to do — high-value semantic signal).
    - Diff: existing "ToolName on file.py: 'old' → 'new'" format.
    - Verification: if a clean tool_result followed the fix, a short
      signal that the fix actually worked.

    Returns "" when fix_event is None — such episodes are kept in SQLite
    for forensic access but filtered out of the vector embedding path.
    """
    if fix_event is None:
        return ""

    parts: list[str] = []
    budget = max_chars

    # Intent — prefix with label so the embedding treats it structurally
    if intent_text:
        intent_budget = min(len(intent_text), budget // 2)
        parts.append(f"Intent: {intent_text[:intent_budget]}")
        budget -= intent_budget + 10

    # Mechanical diff — existing format, grounded in real content
    old_s = (fix_event.old_content or "")[:120]
    new_s = (fix_event.new_content or "")[:120]
    fname = Path(fix_event.file_path or "").name if fix_event.file_path else "?"
    tool = fix_event.tool_name or "Edit"
    if old_s or new_s:
        parts.append(f"{tool} on {fname}: '{old_s}' → '{new_s}'")
    else:
        # Write operations have only new_content; edits to brand-new
        # files have no old_content. Still useful to note the file.
        parts.append(f"{tool} on {fname}")
    budget -= len(parts[-1]) + 10

    # Verification — short signal the fix held
    if verification_event and budget > 40:
        vcontent = (verification_event.content or "").strip()
        if vcontent:
            parts.append(f"Verified: {vcontent[:min(120, budget)]}")
        else:
            parts.append("Verified: clean tool result")

    return ". ".join(parts)
