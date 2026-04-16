"""
Episode search — find problem/fix episodes matching a query.

Semantic-first: searches the ChromaDB `episodes` collection for intent-level
matches, then enriches from SQLite. Optional `keyword` acts as a post-filter
for callers that want literal substring narrowing, but it is never the
primary selector.
"""

from __future__ import annotations

from typing import Any

from longhand.storage.store import LonghandStore


def find_episodes(
    store: LonghandStore,
    query: str | None = None,
    project_ids: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    keyword: str | None = None,
    has_fix: bool = True,
    status: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find problem→fix episodes semantically matching `query`.

    - `query`: natural-language intent (e.g. "the hardest bug we hit").
      When None or empty, falls back to structured SQL filtering only.
    - `project_ids`: restrict to these projects
    - `since` / `until`: ISO timestamps for the episode `ended_at` window
    - `keyword`: optional literal substring post-filter on problem/fix text
    - `has_fix`: only return episodes with a fix_event_id set
    - `status`: 'resolved' | 'partial' | 'unresolved'

    Returns a list of enriched episode dicts (full SQLite rows) with a
    `_distance` key attached when the result came from semantic search.
    Best matches first.
    """
    # Pure-structured path: no query text → just SQL filters
    if not query or not query.strip():
        episodes = store.sqlite.query_episodes(
            project_ids=project_ids,
            since=since,
            until=until,
            status=status,
            keyword=keyword,
            limit=limit * 2,
        )
        if has_fix:
            episodes = [e for e in episodes if e.get("fix_event_id")]
        return episodes[:limit]

    # Semantic path — search the episodes collection, then enrich from SQLite.
    # Phase 1: fetch extra for post-filtering
    try:
        hits = store.vectors.search_episodes(
            query=query,
            n_results=limit * 3,
            since=since,
            until=until,
            has_fix=has_fix if has_fix else None,
        )
    except Exception:
        hits = []

    if not hits:
        # Graceful fallback to structured filtering — better to return
        # something than nothing when the vector collection is cold
        # (e.g. pre-backfill state).
        episodes = store.sqlite.query_episodes(
            project_ids=project_ids,
            since=since,
            until=until,
            status=status,
            keyword=keyword,
            limit=limit,
        )
        if has_fix:
            episodes = [e for e in episodes if e.get("fix_event_id")]
        return episodes[:limit]

    # Phase 2: post-filter by project_ids (ChromaDB can't always $in efficiently)
    if project_ids:
        project_set = set(project_ids)
        hits = [
            h for h in hits
            if (h.get("metadata") or {}).get("project_id") in project_set
        ]

    # Phase 3: enrich from SQLite — preserve semantic ranking
    enriched: list[dict[str, Any]] = []
    distance_map = {
        h.get("episode_id"): h.get("distance", 1.0)
        for h in hits
        if h.get("episode_id")
    }

    for hit in hits[:limit * 2]:
        eid = hit.get("episode_id")
        if not eid:
            continue
        ep = store.sqlite.get_episode(eid)
        if ep is None:
            continue
        if has_fix and not ep.get("fix_event_id"):
            continue
        if status and ep.get("status") != status:
            continue
        # Optional keyword post-filter on the enriched row
        if keyword:
            haystack = " ".join(
                (ep.get(f) or "")
                for f in ("problem_description", "diagnosis_summary", "fix_summary")
            ).lower()
            if keyword.lower() not in haystack:
                continue
        ep["_distance"] = distance_map.get(eid, 1.0)
        enriched.append(ep)

    # Sort by distance (best match first) — ChromaDB returns sorted,
    # but keyword/project/has_fix filters may have reordered things.
    enriched.sort(key=lambda e: e.get("_distance", 1.0))

    return enriched[:limit]
