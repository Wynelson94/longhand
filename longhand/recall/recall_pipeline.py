"""
Recall pipeline — the orchestrator.

Takes a fuzzy natural-language query and returns a RecallResult with:
- project matches
- time window
- ranked episodes
- artifacts (diffs, reconstructed file states, verbatim thinking blocks)
- narrative (prebuilt markdown story)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from longhand.recall.episode_search import find_episodes
from longhand.recall.narrative import build_narrative
from longhand.recall.project_match import ProjectMatch, match_projects
from longhand.recall.time_parser import parse_time_phrase
from longhand.replay import ReplayEngine
from longhand.storage.store import LonghandStore


@dataclass
class RecallResult:
    query: str
    project_matches: list[ProjectMatch] = field(default_factory=list)
    time_window: tuple[datetime | None, datetime | None] = (None, None)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    narrative: str = ""


def _load_episode_artifacts(store: LonghandStore, episode: dict[str, Any]) -> dict[str, Any]:
    """Load the supporting artifacts for a single episode.

    Returns a dict with:
      fix.old, fix.new, fix.file_path, fix.thinking_block,
      fix.file_state_after (reconstructed via ReplayEngine)
    """
    artifacts: dict[str, Any] = {"fix": {}, "diagnosis": None}

    fix_id = episode.get("fix_event_id")
    if fix_id:
        fix_event = store.sqlite.get_event(fix_id)
        if fix_event:
            artifacts["fix"] = {
                "tool_name": fix_event.get("tool_name"),
                "file_path": fix_event.get("file_path"),
                "old": fix_event.get("old_content") or "",
                "new": fix_event.get("new_content") or "",
                "event_id": fix_id,
            }

            # Reconstruct the file state immediately after the fix
            if fix_event.get("file_path") and episode.get("session_id"):
                engine = ReplayEngine(store.sqlite)
                try:
                    state = engine.file_state_at(
                        file_path=fix_event["file_path"],
                        session_id=episode["session_id"],
                        at_event_id=fix_id,
                    )
                    if state:
                        artifacts["fix"]["file_state_after"] = state.content
                except Exception:
                    pass

    diag_id = episode.get("diagnosis_event_id")
    if diag_id:
        diag_event = store.sqlite.get_event(diag_id)
        if diag_event:
            artifacts["diagnosis"] = diag_event.get("content")

    return artifacts


def recall(
    store: LonghandStore,
    query: str,
    now: datetime | None = None,
    max_episodes: int = 5,
) -> RecallResult:
    """Full recall pipeline.

    1. Parse time phrase → (since, until)
    2. Match projects on the remaining query
    3. Find episodes with filters
    4. If nothing found, relax filters and retry
    5. Rank and load artifacts for top episodes
    6. Build narrative
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. Time parsing
    since, until, cleaned_query = parse_time_phrase(query, now)

    # 2. Project matching on the cleaned query
    project_matches = match_projects(store, cleaned_query, top_k=5, now=now)

    # If project matching is weak (top score < 1.0), augment with semantic
    # session search — find sessions whose summary embedding matches the query
    # and treat their project_ids as candidates.
    top_project_score = project_matches[0].score if project_matches else 0.0
    session_project_ids: set[str] = set()
    if top_project_score < 1.5 and cleaned_query.strip():
        try:
            session_hits = store.vectors.search_sessions(
                query=cleaned_query,
                n_results=10,
                since=since.isoformat() if since else None,
                until=until.isoformat() if until else None,
            )
            for hit in session_hits:
                meta = hit.get("metadata") or {}
                pid = meta.get("project_id")
                if pid:
                    session_project_ids.add(pid)
        except Exception:
            pass

    project_ids: list[str] | None = None
    if project_matches:
        project_ids = [m.project_id for m in project_matches]
        # Merge in project ids from semantic session search
        if session_project_ids:
            existing = set(project_ids)
            for pid in session_project_ids:
                if pid not in existing:
                    project_ids.append(pid)
    elif session_project_ids:
        project_ids = list(session_project_ids)

    since_iso = since.isoformat() if since else None
    until_iso = until.isoformat() if until else None

    # Extract candidate keywords from the cleaned query (longest words are most distinctive)
    import re as _re
    query_words = sorted(
        [w for w in _re.findall(r"[a-zA-Z][a-zA-Z0-9]{3,}", cleaned_query) if w.lower() not in {"when", "what", "that", "this", "with", "from", "where", "made", "done", "last", "couple", "months", "weeks", "days", "time", "some", "thing", "about"}],
        key=len,
        reverse=True,
    )
    primary_keyword = query_words[0] if query_words else None

    # 3. First attempt: strict filters with keyword hint
    episodes = find_episodes(
        store=store,
        project_ids=project_ids,
        since=since_iso,
        until=until_iso,
        keyword=primary_keyword,
        has_fix=True,
        limit=max_episodes * 4,
    )

    # 4. Relax: drop keyword
    if not episodes:
        episodes = find_episodes(
            store=store,
            project_ids=project_ids,
            since=since_iso,
            until=until_iso,
            has_fix=True,
            limit=max_episodes * 4,
        )

    # 5. Relax: drop project filter, keep keyword
    if not episodes and primary_keyword:
        episodes = find_episodes(
            store=store,
            project_ids=None,
            since=since_iso,
            until=until_iso,
            keyword=primary_keyword,
            has_fix=True,
            limit=max_episodes * 4,
        )

    # 6. Relax: drop project, has_fix
    if not episodes:
        episodes = find_episodes(
            store=store,
            project_ids=None,
            since=since_iso,
            until=until_iso,
            has_fix=False,
            limit=max_episodes * 4,
        )

    # 7. Semantic re-ranking — use the events vector store to find events most
    # similar to the query, then boost episodes whose problem/diagnosis/fix
    # events appear in that set.
    semantic_event_scores: dict[str, float] = {}
    if cleaned_query.strip():
        try:
            event_hits = store.vectors.search(
                query=cleaned_query,
                n_results=50,
            )
            for hit in event_hits:
                eid = hit.get("event_id")
                if eid:
                    distance = hit.get("distance", 1.0)
                    semantic_event_scores[eid] = max(0.0, 1.0 - distance)
        except Exception:
            pass

    def _rank_score(ep: dict[str, Any]) -> float:
        confidence = ep.get("confidence") or 0.5

        # Keyword hit count — substring match on problem/diagnosis/fix text
        keyword_hits = 0
        if query_words:
            searchable = " ".join([
                ep.get("problem_description") or "",
                ep.get("diagnosis_summary") or "",
                ep.get("fix_summary") or "",
            ]).lower()
            for word in query_words[:5]:
                if word.lower() in searchable:
                    keyword_hits += 1

        # Semantic boost — check if any linked event is in the top semantic hits
        semantic_boost = 0.0
        for eid_field in ("problem_event_id", "diagnosis_event_id", "fix_event_id"):
            eid = ep.get(eid_field)
            if eid and eid in semantic_event_scores:
                semantic_boost = max(semantic_boost, semantic_event_scores[eid])

        # Recency boost — more recent episodes rank higher for fuzzy time queries
        recency_boost = 0.0
        ended_at = ep.get("ended_at")
        if ended_at:
            try:
                ep_time = datetime.fromisoformat(ended_at)
                if ep_time.tzinfo is None:
                    ep_time = ep_time.replace(tzinfo=timezone.utc)
                days_ago = max(1, (now - ep_time).days)
                recency_boost = max(0.0, 1.0 - (days_ago / 365))
            except Exception:
                pass

        # Final: keyword hits dominate (10x), then semantic boost (5x),
        # then confidence + recency
        return keyword_hits * 10 + semantic_boost * 5 + confidence + recency_boost * 0.5

    episodes = sorted(episodes, key=_rank_score, reverse=True)[:max_episodes]

    # 8. Load artifacts for the top episode
    artifacts: dict[str, Any] = {}
    if episodes:
        artifacts = _load_episode_artifacts(store, episodes[0])

    # 9. Build narrative
    narrative = build_narrative(
        query=query,
        project_matches=project_matches,
        episodes=episodes,
        artifacts=artifacts,
        time_window=(since, until),
    )

    return RecallResult(
        query=query,
        project_matches=project_matches,
        time_window=(since, until),
        episodes=episodes,
        artifacts=artifacts,
        narrative=narrative,
    )
