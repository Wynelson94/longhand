"""
Episode search — find problem/fix episodes matching a query.

Combines SQLite structured filters (project, time, status) with optional
semantic re-ranking on the episode's problem_description + diagnosis_summary.
"""

from __future__ import annotations

from typing import Any

from longhand.storage.store import LonghandStore


def find_episodes(
    store: LonghandStore,
    project_ids: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    keyword: str | None = None,
    has_fix: bool = True,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Query episodes with structured filters.

    - `project_ids`: limit to these projects (None = all)
    - `since` / `until`: ISO timestamps for the episode `ended_at` window
    - `keyword`: substring match on problem/diagnosis/fix summaries
    - `has_fix`: only return episodes with a fix_event_id set
    - `status`: 'resolved' | 'partial' | 'unresolved'
    """
    episodes = store.sqlite.query_episodes(
        project_ids=project_ids,
        since=since,
        until=until,
        status=status,
        keyword=keyword,
        limit=limit * 2,  # grab extra for post-filtering
    )

    if has_fix:
        episodes = [e for e in episodes if e.get("fix_event_id")]

    return episodes[:limit]
