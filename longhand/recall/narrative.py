"""
Deterministic narrative synthesis.

Takes a RecallResult and produces a markdown story the user (or Claude) can read.
No LLM. Pure template fill.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from longhand.recall.project_match import ProjectMatch


def _humanize_timestamp(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        days = delta.days
        if days == 0:
            return "today"
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days} days ago"
        if days < 30:
            weeks = days // 7
            return f"{weeks} week{'s' if weeks > 1 else ''} ago"
        if days < 365:
            months = days // 30
            return f"{months} month{'s' if months > 1 else ''} ago"
        years = days // 365
        return f"{years} year{'s' if years > 1 else ''} ago"
    except Exception:
        return iso[:10]


def build_narrative(
    query: str,
    project_matches: list[ProjectMatch],
    episodes: list[dict[str, Any]],
    artifacts: dict[str, Any],
    time_window: tuple[datetime | None, datetime | None] | None = None,
    segments: list[dict[str, Any]] | None = None,
    fallback_snippets: list[dict[str, Any]] | None = None,
    secondary_segments: list[dict[str, Any]] | None = None,
) -> str:
    """Produce a markdown narrative from the recall results."""
    lines: list[str] = []

    lines.append(f"**You asked:** _{query.strip()}_\n")

    # If segments are the primary result, render segment narrative
    if segments:
        return _build_segment_narrative(lines, query, project_matches, segments)

    # If fallback snippets are the primary result, render fallback narrative
    if fallback_snippets:
        return _build_fallback_narrative(lines, query, project_matches, fallback_snippets)

    if not episodes:
        lines.append("_No matching episodes found in Longhand memory._\n")
        if project_matches:
            lines.append("**Projects I considered:**")
            for pm in project_matches[:3]:
                lines.append(f"- **{pm.display_name}** ({pm.category or 'uncategorized'}) — {', '.join(pm.reasons)}")
        return "\n".join(lines)

    top = episodes[0]

    # Header
    project_name = "unknown project"
    if project_matches:
        project_name = project_matches[0].display_name

    when = _humanize_timestamp(top.get("started_at"))
    session_short = (top.get("session_id") or "")[:8]
    lines.append(f"**Found it:** {project_name} · {when} · session `{session_short}`\n")

    # Problem
    if top.get("problem_description"):
        lines.append("### What went wrong")
        lines.append(f"{top['problem_description'].strip()}\n")

    # Diagnosis (verbatim thinking block)
    if top.get("diagnosis_summary"):
        lines.append("### How it was diagnosed")
        lines.append("```")
        lines.append(top["diagnosis_summary"].strip())
        lines.append("```\n")

    # Fix
    if top.get("fix_summary"):
        lines.append("### The fix")
        lines.append(f"{top['fix_summary']}\n")

    # Diff artifact
    fix = artifacts.get("fix") if artifacts else None
    if fix and (fix.get("old") or fix.get("new")):
        lines.append("**Diff:**")
        lines.append("```diff")
        old_lines = (fix.get("old") or "").splitlines() or [""]
        new_lines = (fix.get("new") or "").splitlines() or [""]
        for line in old_lines:
            lines.append(f"- {line}")
        for line in new_lines:
            lines.append(f"+ {line}")
        lines.append("```\n")

    # Touched files
    touched_raw = top.get("touched_files_json")
    if touched_raw:
        try:
            touched = json.loads(touched_raw)
            if touched:
                lines.append(f"**Files touched:** {', '.join(touched[:5])}\n")
        except Exception:
            pass

    # Verification
    if top.get("verification_event_id"):
        lines.append("✓ **Verified** — a test or command succeeded after the fix.\n")
    elif top.get("status") == "unresolved":
        lines.append("⚠ **Unresolved** — we didn't find a verification event for this fix.\n")

    # Other candidates
    if len(episodes) > 1:
        lines.append(f"### Other candidates ({len(episodes) - 1})")
        for ep in episodes[1:4]:
            ep_when = _humanize_timestamp(ep.get("started_at"))
            summary = (ep.get("problem_description") or "")[:100]
            lines.append(f"- {ep_when}: {summary}")

    # Secondary matches in other sessions — recall used to drop these silently
    # when episodes won the primary slot, leaving the user unaware of weaker
    # but still relevant hits. Surface them so they can chase the lead.
    if secondary_segments:
        lines.append(f"\n### Also possibly relevant ({len(secondary_segments)})")
        lines.append("_Weaker matches in other sessions — chase if the primary doesn't answer:_")
        for seg in secondary_segments[:3]:
            seg_short = (seg.get("session_id") or "")[:8]
            seg_when = _humanize_timestamp(seg.get("started_at"))
            seg_topic = (seg.get("topic") or seg.get("summary") or "")[:120].replace("\n", " ")
            lines.append(f"- session `{seg_short}` ({seg_when}): {seg_topic}")
        lines.append(
            f'[Use `search_in_context("<session>", "{query[:50]}")` to read full context.]'
        )

    return "\n".join(lines)


def _build_segment_narrative(
    lines: list[str],
    query: str,
    project_matches: list[ProjectMatch],
    segments: list[dict[str, Any]],
) -> str:
    """Build a narrative from conversation segment results."""
    top = segments[0]

    # Header
    project_name = "unknown project"
    if project_matches:
        project_name = project_matches[0].display_name
    when = _humanize_timestamp(top.get("started_at"))
    session_short = (top.get("session_id") or "")[:8]

    lines.append(f"**Found it:** {project_name} · {when} · session `{session_short}`\n")

    # Conversation topic
    topic = top.get("topic", "")
    if topic:
        lines.append(f"### Conversation: {topic[:100]}\n")

    # Summary
    summary = top.get("summary", "")
    if summary:
        lines.append(summary[:800] + "\n")

    # Metadata
    seg_type = top.get("segment_type", "discussion")
    keywords_raw = top.get("keywords_json") or top.get("keywords") or "[]"
    try:
        keywords = json.loads(keywords_raw) if isinstance(keywords_raw, str) else (keywords_raw or [])
    except Exception:
        keywords = []
    if keywords:
        lines.append(f"**Type:** {seg_type} · **Keywords:** {', '.join(keywords[:8])}")

    event_count = top.get("event_count", 0)
    start = _humanize_timestamp(top.get("started_at"))
    end = _humanize_timestamp(top.get("ended_at"))
    lines.append(f"**Duration:** {start} to {end} ({event_count} events)\n")

    # Drill-down hint
    lines.append(
        f'[Use `search_in_context("{session_short}", "{query[:50]}")` '
        f"to read the full conversation.]\n"
    )

    # Other segment candidates — include session id so the user can drill in.
    # The id was previously omitted, which silently hid which session each
    # other-match came from (caught by the secondary-match canary).
    if len(segments) > 1:
        lines.append(f"### Other matches ({len(segments) - 1})")
        for seg in segments[1:4]:
            seg_short = (seg.get("session_id") or "")[:8]
            seg_when = _humanize_timestamp(seg.get("started_at"))
            seg_topic = (seg.get("topic") or "")[:80]
            lines.append(f"- session `{seg_short}` ({seg_when}): {seg_topic}")

    return "\n".join(lines)


def _build_fallback_narrative(
    lines: list[str],
    query: str,
    project_matches: list[ProjectMatch],
    fallback_snippets: list[dict[str, Any]],
) -> str:
    """Build a narrative from event-level fallback results."""
    lines.append(
        "_No episodes or conversation segments matched directly. "
        "Closest event-level matches:_\n"
    )

    for snippet in fallback_snippets[:3]:
        session_short = (snippet.get("session_id") or "")[:8]
        when = _humanize_timestamp(snippet.get("timestamp"))
        content = (snippet.get("content") or "")[:300]

        lines.append(f"### From session `{session_short}` ({when})")
        lines.append(f"> {content}\n")
        lines.append(
            f'[Use `search_in_context("{session_short}", "{query[:50]}")` '
            f"for full context.]\n"
        )

    return "\n".join(lines)


def build_project_status_narrative(
    display_name: str,
    canonical_path: str,
    last_commits: list[dict[str, Any]],
    active_branch: str | None,
    recent_sessions: list[dict[str, Any]],
    recent_episodes: list[dict[str, Any]],
    unresolved_episodes: list[dict[str, Any]],
    recent_segments: list[dict[str, Any]],
    last_outcome: dict[str, Any] | None,
    latest_fix_summary: str | None = None,
) -> str:
    """Build a 'here's where you left off' narrative for a project.

    Degrades gracefully — sections are omitted when data is missing.
    Works for projects with zero git history.
    """
    lines: list[str] = []

    # Header
    branch_str = f" · branch: `{active_branch}`" if active_branch else ""
    lines.append(f"## {display_name}")
    lines.append(f"`{canonical_path}`{branch_str}\n")

    # Last session
    if recent_sessions:
        last_session = recent_sessions[0]
        when = _humanize_timestamp(last_session.get("started_at"))
        event_count = last_session.get("event_count", 0)
        outcome_str = ""
        if last_outcome:
            outcome_str = f"Outcome: **{last_outcome.get('outcome', 'unknown')}**"
            # session_outcomes.summary is sourced from the first user message
            # of the session, not a fix description — it's the wrong field for
            # a narrative trailer. Use the most-recent episode's fix_summary
            # instead when available.
            if latest_fix_summary:
                outcome_str += f" · Last fix: {latest_fix_summary[:150]}"
            outcome_str += "\n"
        lines.append("### Last session")
        lines.append(f"{outcome_str}{when} · {event_count} events\n")

    # Recent commits — skip rows with no parseable hash (legacy data) so the
    # narrative doesn't show blank backticks.
    rendered_commits = [
        c for c in last_commits[:10] if (c.get("commit_hash") or "").strip()
    ]
    if rendered_commits:
        lines.append(f"### Recent commits ({len(rendered_commits)})")
        for commit in rendered_commits:
            hash_short = commit["commit_hash"][:8]
            message = (commit.get("commit_message") or "no message")[:80]
            when = _humanize_timestamp(commit.get("timestamp"))

            line = f"- `{hash_short}` {message} ({when})"

            # Show linked episode if exists
            linked = commit.get("linked_episode")
            if linked:
                fix = (linked.get("fix_summary") or "")[:80]
                if fix:
                    line += f"\n  linked: {fix}"

            lines.append(line)
        lines.append("")

    # Known issues (unresolved episodes)
    if unresolved_episodes:
        lines.append(f"### Known issues ({len(unresolved_episodes)})")
        for ep in unresolved_episodes[:5]:
            problem = (ep.get("problem_description") or "unknown issue")[:120]
            when = _humanize_timestamp(ep.get("ended_at"))
            lines.append(f"- {problem} ({when})")
        lines.append("")
    else:
        lines.append("### Known issues")
        lines.append("None tracked.\n")

    # Recent work (conversation segments)
    if recent_segments:
        lines.append(f"### Recent work ({len(recent_segments)})")
        for seg in recent_segments[:5]:
            topic = (seg.get("topic") or "")[:80]
            seg_type = seg.get("segment_type", "discussion")
            when = _humanize_timestamp(seg.get("ended_at"))
            lines.append(f"- [{seg_type}] {topic} ({when})")
        lines.append("")

    # No data at all
    if not last_commits and not recent_sessions and not recent_episodes:
        lines.append("_No session history found for this project._")

    return "\n".join(lines)
