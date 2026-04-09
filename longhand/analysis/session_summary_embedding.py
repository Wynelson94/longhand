"""
Build session-level summary text that gets embedded into ChromaDB's `sessions`
collection for fuzzy recall. One embedding per session.

Deterministic template. No LLM.
"""

from __future__ import annotations

import json
from typing import Any

from longhand.types import Event, Session


def build_session_text(
    session: Session,
    events: list[Event],
    outcome: dict[str, Any],
    project: dict[str, Any] | None,
) -> str:
    """Assemble the one-paragraph text that will represent this session in vector search."""
    parts: list[str] = []

    if project:
        parts.append(f"Project: {project.get('display_name', 'unknown')}")
        if project.get("category"):
            parts.append(f"Category: {project['category']}")
        keywords = project.get("keywords") or []
        if keywords:
            parts.append(f"Keywords: {', '.join(keywords[:10])}")

    # First real user message
    first_user = ""
    for e in events:
        etype = e.event_type if isinstance(e.event_type, str) else e.event_type.value
        if etype == "user_message" and e.content and e.content.strip():
            first_user = e.content.strip()
            break

    if first_user:
        parts.append(f"Asked: {first_user[:300]}")

    parts.append(f"Outcome: {outcome.get('outcome', 'unknown')}")
    parts.append(f"Summary: {outcome.get('summary', '')[:300]}")

    topics = outcome.get("topics") or []
    if topics:
        parts.append(f"Topics: {', '.join(topics[:8])}")

    # Files touched
    touched = sorted({e.file_path for e in events if e.file_path})[:10]
    if touched:
        parts.append(f"Files: {', '.join(touched)}")

    return " | ".join(parts)


def build_session_metadata(
    session: Session,
    outcome: dict[str, Any],
    project: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the metadata dict stored with the session embedding (Chroma where filters)."""
    meta: dict[str, Any] = {
        "session_id": session.session_id,
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat(),
        "outcome": outcome.get("outcome", "unknown"),
    }
    if project:
        meta["project_id"] = project["project_id"]
        if project.get("category"):
            meta["category"] = project["category"]
    return meta


def build_project_text(project: dict[str, Any]) -> str:
    """Text for the per-project embedding in Chroma's `projects` collection."""
    parts = [project.get("display_name", "")]
    if project.get("category"):
        parts.append(f"({project['category']})")
    parts.append("Aliases: " + ", ".join(project.get("aliases", [])[:10]))
    parts.append("Keywords: " + ", ".join(project.get("keywords", [])[:20]))
    parts.append("Languages: " + ", ".join(project.get("languages", [])))
    return " | ".join(parts)
