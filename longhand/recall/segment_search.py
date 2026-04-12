"""
Segment search for the recall pipeline.

Searches conversation segments (the non-episode retrieval unit) using
semantic search + structured filtering, same two-phase pattern as
episode search.
"""

from __future__ import annotations

from typing import Any

from longhand.storage.store import LonghandStore


def find_segments(
    store: LonghandStore,
    query: str,
    project_ids: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Find conversation segments matching a query.

    1. Semantic search via ChromaDB segments collection
    2. Post-filter by project_ids and time window
    3. Enrich with full SQLite row data
    4. Return scored results
    """
    # Phase 1: semantic search — fetch extra for post-filtering
    try:
        hits = store.vectors.search_segments(
            query=query,
            n_results=limit * 3,
            since=since,
            until=until,
        )
    except Exception:
        hits = []

    if not hits:
        return []

    # Phase 2: post-filter by project_ids if specified
    if project_ids:
        project_set = set(project_ids)
        hits = [
            h for h in hits
            if (h.get("metadata") or {}).get("project_id") in project_set
        ]

    # Phase 3: enrich from SQLite — single connection for all lookups
    enriched: list[dict[str, Any]] = []
    seg_ids = [h.get("segment_id") for h in hits[:limit] if h.get("segment_id")]
    if not seg_ids:
        return []

    # Build a distance map for later attachment
    distance_map = {
        h.get("segment_id"): h.get("distance", 1.0)
        for h in hits[:limit]
        if h.get("segment_id")
    }

    try:
        with store.sqlite.connect() as conn:
            placeholders = ",".join(["?"] * len(seg_ids))
            rows = conn.execute(
                f"SELECT * FROM conversation_segments WHERE segment_id IN ({placeholders})",
                seg_ids,
            ).fetchall()
            for row in rows:
                segment = dict(row)
                segment["_distance"] = distance_map.get(segment["segment_id"], 1.0)
                enriched.append(segment)
    except Exception:
        # If SQLite lookup fails (e.g., table doesn't exist pre-migration),
        # fall back to vector hit metadata
        for hit in hits[:limit]:
            seg_id = hit.get("segment_id")
            if not seg_id:
                continue
            enriched.append({
                "segment_id": seg_id,
                "session_id": (hit.get("metadata") or {}).get("session_id", ""),
                "segment_type": (hit.get("metadata") or {}).get("segment_type", "discussion"),
                "summary": hit.get("document", ""),
                "_distance": hit.get("distance", 1.0),
            })

    # Sort by distance (best match first) to preserve semantic ranking
    enriched.sort(key=lambda s: s.get("_distance", 1.0))

    return enriched
