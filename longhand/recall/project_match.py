"""
Fuzzy project matching for recall queries.

Multi-stage:
1. Exact alias substring against aliases_json / display_name
2. Category hit ("game", "app", "cli", etc.)
3. Semantic fallback via the `projects` ChromaDB collection
4. Recency boost (log-decay)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from longhand.storage.store import LonghandStore

# Category terms that can appear in queries
_CATEGORY_TERMS = {
    "game": "game",
    "games": "game",
    "app": "web app",
    "webapp": "web app",
    "website": "web app",
    "site": "web app",
    "cli": "cli tool",
    "tool": "cli tool",
    "api": "python web",
    "service": "go service",
    "crm": "crm",
    "mobile": "mobile app",
    "ios": "mobile app",
    "android": "mobile app",
}


@dataclass
class ProjectMatch:
    project_id: str
    display_name: str
    category: str | None
    canonical_path: str
    score: float
    reasons: list[str]


def _recency_boost(last_seen_iso: str, now: datetime) -> float:
    """Log-decay recency: recent → 1.0, old → trails to 0.3."""
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        days_ago = max(1, (now - last_seen).days)
        # e.g. 1 day ago → ~1.0, 30 days → ~0.8, 365 days → ~0.4
        return max(0.3, 1.0 - 0.15 * math.log10(days_ago + 1))
    except Exception:
        return 0.5


def match_projects(
    store: LonghandStore,
    query: str,
    top_k: int = 5,
    now: datetime | None = None,
    _allow_fallback: bool = True,
) -> list[ProjectMatch]:
    """Return top-k projects matching the query.

    If the first pass returns no results and `_allow_fallback` is True,
    we run a cheap on-the-fly project-inference pass over un-indexed
    session files (see `project_fallback`) and re-run the match. The
    recursive call passes `_allow_fallback=False` to prevent loops.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    query_lower = query.lower().strip()
    if not query_lower:
        return []

    # Load all projects — usually small enough (dozens to low hundreds)
    all_projects = store.sqlite.list_projects(limit=1000)
    if not all_projects and _allow_fallback:
        return _fallback_match(store, query, top_k, now)

    scored: dict[str, ProjectMatch] = {}

    # Stage 1: exact alias substring match
    for proj in all_projects:
        reasons: list[str] = []
        score = 0.0

        aliases = set(json.loads(proj.get("aliases_json") or "[]"))
        display_lower = proj["display_name"].lower()

        # Check each whole word in the query against aliases and display
        query_words = re.findall(r"[a-z0-9-_]{3,}", query_lower)
        for word in query_words:
            if word in aliases or word in display_lower:
                score += 2.0
                reasons.append(f"alias: '{word}'")
                break

        if score > 0:
            recency = _recency_boost(proj["last_seen"], now)
            score *= recency
            reasons.append(f"recency: {recency:.2f}")
            scored[proj["project_id"]] = ProjectMatch(
                project_id=proj["project_id"],
                display_name=proj["display_name"],
                category=proj.get("category"),
                canonical_path=proj["canonical_path"],
                score=score,
                reasons=reasons,
            )

    # Stage 2: category hit
    for term, category in _CATEGORY_TERMS.items():
        if re.search(rf"\b{re.escape(term)}\b", query_lower):
            for proj in all_projects:
                if proj.get("category") == category:
                    pid = proj["project_id"]
                    if pid in scored:
                        scored[pid].score += 0.5
                        scored[pid].reasons.append(f"category: {category}")
                    else:
                        recency = _recency_boost(proj["last_seen"], now)
                        scored[pid] = ProjectMatch(
                            project_id=pid,
                            display_name=proj["display_name"],
                            category=proj.get("category"),
                            canonical_path=proj["canonical_path"],
                            score=1.5 * recency,
                            reasons=[f"category: {category}", f"recency: {recency:.2f}"],
                        )

    # Stage 3: semantic fallback via projects collection
    try:
        semantic_hits = store.vectors.search_projects(query=query, n_results=top_k * 2)
        for hit in semantic_hits:
            pid = hit["project_id"]
            distance = hit.get("distance", 1.0)
            semantic_score = max(0.0, 1.0 - distance)
            if semantic_score < 0.15:
                continue  # too weak
            if pid in scored:
                scored[pid].score += semantic_score
                scored[pid].reasons.append(f"semantic: {semantic_score:.2f}")
            else:
                proj = store.sqlite.get_project(pid)
                if proj:
                    recency = _recency_boost(proj["last_seen"], now)
                    scored[pid] = ProjectMatch(
                        project_id=pid,
                        display_name=proj["display_name"],
                        category=proj.get("category"),
                        canonical_path=proj["canonical_path"],
                        score=semantic_score * recency,
                        reasons=[f"semantic: {semantic_score:.2f}", f"recency: {recency:.2f}"],
                    )
    except Exception:
        pass

    # Sort and return top-k
    results = sorted(scored.values(), key=lambda m: m.score, reverse=True)
    if not results and _allow_fallback:
        return _fallback_match(store, query, top_k, now)
    return results[:top_k]


def _fallback_match(
    store: LonghandStore,
    query: str,
    top_k: int,
    now: datetime,
) -> list[ProjectMatch]:
    """Run on-the-fly project inference, then retry the match once.

    Fires a detached background `longhand ingest` so semantic /
    episode / segment data catches up for subsequent queries. Any matches
    found via this path get a 'on-the-fly inference' reason tag so the
    caller can surface the caveat.
    """
    # Import lazily to avoid a circular import at module load.
    from longhand.recall.project_fallback import (
        infer_missing_projects,
        trigger_background_ingest,
    )

    inferred = infer_missing_projects(store)

    # Regardless of whether anything was inferred, kick off a full
    # background ingest so any future query sees richer data. No-op if
    # another ingest is already running.
    trigger_background_ingest(store)

    if not inferred:
        return []

    results = match_projects(store, query, top_k=top_k, now=now, _allow_fallback=False)
    for match in results:
        match.reasons.append(
            "on-the-fly inference (semantic index catching up in background)"
        )
    return results
