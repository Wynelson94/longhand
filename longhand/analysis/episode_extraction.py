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


def _get_op(e: Event) -> str | None:
    if e.file_operation is None:
        return None
    return e.file_operation if isinstance(e.file_operation, str) else e.file_operation.value


def _get_type(e: Event) -> str:
    return e.event_type if isinstance(e.event_type, str) else e.event_type.value


def _episode_id(session_id: str, start_idx: int) -> str:
    return "ep_" + hashlib.sha256(f"{session_id}:{start_idx}".encode("utf-8")).hexdigest()[:16]


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

        # Walk backward for the most recent user_message (the request)
        preceding_user = None
        for j in range(i - 1, max(i - 10, -1), -1):
            if _get_type(events[j]) == "user_message" and (events[j].content or "").strip():
                preceding_user = events[j]
                break

        problem_description = (preceding_user.content[:500] if preceding_user else (e.error_snippet or e.content[:500] if e.content else ""))

        # Forward scan
        diagnosis_event = None
        fix_event = None
        verification_event = None
        touched_files: set[str] = set(problem_files)
        tags: set[str] = set()
        if e.error_category:
            tags.add(e.error_category)

        j = i + 1
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

            # Diagnosis: thinking block that mentions the error or referenced files
            if ctype == "assistant_thinking" and diagnosis_event is None:
                content = (cand.content or "").lower()
                if e.error_snippet:
                    error_keywords = _extract_keywords(e.error_snippet)[:5]
                    if any(word in content for word in error_keywords):
                        diagnosis_event = cand
                if diagnosis_event is None and problem_files:
                    if any(Path(f).name.lower() in content for f in problem_files):
                        diagnosis_event = cand

            # Fix: tool_call that edits a file referenced in the error
            if ctype == "tool_call" and _get_op(cand) in _FIX_OPERATIONS:
                if cand.file_path:
                    touched_files.add(cand.file_path)
                if fix_event is None:
                    # Prefer fixes to files named in the error
                    if problem_files and cand.file_path and any(
                        Path(ref).name == Path(cand.file_path).name for ref in problem_files
                    ):
                        fix_event = cand
                    # Otherwise, accept any edit after a diagnosis
                    elif diagnosis_event is not None:
                        fix_event = cand

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

        # Fix summary (deterministic)
        fix_summary = ""
        if fix_event:
            old_s = (fix_event.old_content or "")[:80]
            new_s = (fix_event.new_content or "")[:80]
            fname = Path(fix_event.file_path or "").name if fix_event.file_path else "?"
            fix_summary = f"{fix_event.tool_name or 'Edit'} on {fname}: '{old_s}' → '{new_s}'"

        diagnosis_summary = ""
        if diagnosis_event and diagnosis_event.content:
            diagnosis_summary = diagnosis_event.content[:400]

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
