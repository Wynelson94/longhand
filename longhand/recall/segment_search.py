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

    # Phase 3: enrich from SQLite (full segment row with topic, keywords, etc.)
    enriched: list[dict[str, Any]] = []
    for hit in hits[:limit]:
        seg_id = hit.get("segment_id")
        if not seg_id:
            continue

        # Look up the full segment row from SQLite
        rows = store.sqlite.query_segments(
            session_id=None,
            limit=1,
        )
        # Direct lookup by segment_id
        try:
            from longhand.storage.sqlite_store import SQLiteStore
            with store.sqlite.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM conversation_segments WHERE segment_id = ?",
                    (seg_id,),
                ).fetchone()
                if row:
                    segment = dict(row)
                    segment["_distance"] = hit.get("distance", 1.0)
                    enriched.append(segment)
        except Exception:
            # If SQLite lookup fails, use what we have from the vector hit
            enriched.append({
                "segment_id": seg_id,
                "session_id": (hit.get("metadata") or {}).get("session_id", ""),
                "segment_type": (hit.get("metadata") or {}).get("segment_type", "discussion"),
                "summary": hit.get("document", ""),
                "_distance": hit.get("distance", 1.0),
            })

    return enriched
