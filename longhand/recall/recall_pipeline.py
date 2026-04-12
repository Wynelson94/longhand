"""
Recall pipeline — the orchestrator.

Takes a fuzzy natural-language query and returns a RecallResult with:
- project matches
- time window
- ranked episodes
- conversation segments (non-episode matches)
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
from longhand.recall.segment_search import find_segments
from longhand.recall.time_parser import parse_time_phrase
from longhand.replay import ReplayEngine
from longhand.storage.store import LonghandStore


@dataclass
class RecallResult:
    query: str
    project_matches: list[ProjectMatch] = field(default_factory=list)
    time_window: tuple[datetime | None, datetime | None] = (None, None)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    narrative: str = ""


@dataclass
class ProjectStatus:
    """Result of a git-aware project status query."""
    project_id: str
    display_name: str
    canonical_path: str
    category: str | None
    last_commits: list[dict[str, Any]] = field(default_factory=list)
    active_branch: str | None = None
    recent_sessions: list[dict[str, Any]] = field(default_factory=list)
    recent_episodes: list[dict[str, Any]] = field(default_factory=list)
    unresolved_episodes: list[dict[str, Any]] = field(default_factory=list)
    recent_segments: list[dict[str, Any]] = field(default_factory=list)
    last_outcome: dict[str, Any] | None = None
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

    # 7.5. Episode quality gate — detect when episode results are garbage
    # Threshold 15.0 requires either 2+ keyword hits or 1 keyword + strong semantic boost.
    # A single short-word match (e.g., "body" matching a JS variable) scores ~11 and should NOT
    # be enough to override the segment/fallback path.
    best_episode_score = _rank_score(episodes[0]) if episodes else 0.0
    episodes_are_relevant = best_episode_score >= 15.0

    # 7.6. Parallel segment search — always run, results prioritized by quality gate
    segments: list[dict[str, Any]] = []
    if cleaned_query.strip():
        try:
            segments = find_segments(
                store=store,
                query=cleaned_query,
                project_ids=project_ids,
                since=since_iso,
                until=until_iso,
                limit=max_episodes,
            )
        except Exception:
            pass

    # 7.7. Event-level fallback — always run when episodes are irrelevant,
    # regardless of segment quality. If segments exist, they'll be used;
    # if not, fallback snippets provide the answer.
    fallback_snippets: list[dict[str, Any]] = []
    if not episodes_are_relevant and cleaned_query.strip():
        # Run filtered searches to find actual conversations, not tool noise
        # user_message events are the strongest signal for topic recall
        user_hits = store.vectors.search(
            query=cleaned_query, n_results=20, event_type="user_message",
        )
        asst_hits = store.vectors.search(
            query=cleaned_query, n_results=10, event_type="assistant_text",
        )
        # Merge and dedupe by event_id
        seen_ids: set[str] = set()
        conversational: list[dict[str, Any]] = []
        for hit in user_hits + asst_hits:
            eid = hit.get("event_id", "")
            if eid not in seen_ids:
                seen_ids.add(eid)
                conversational.append(hit)

        # Group by session, take top 3 sessions
        session_groups: dict[str, list[dict[str, Any]]] = {}
        for hit in conversational:
            sid = (hit.get("metadata") or {}).get("session_id", "")
            if sid:
                session_groups.setdefault(sid, []).append(hit)
        for sid, hits in sorted(
            session_groups.items(),
            key=lambda x: min(h.get("distance", 1.0) for h in x[1]),
        )[:3]:
            best_hit = min(hits, key=lambda h: h.get("distance", 1.0))
            fallback_snippets.append({
                "session_id": sid,
                "content": best_hit.get("document", "")[:500],
                "event_type": (best_hit.get("metadata") or {}).get("event_type", ""),
                "timestamp": (best_hit.get("metadata") or {}).get("timestamp", ""),
                "_distance": best_hit.get("distance", 1.0),
            })

    # 8. Decide what to present based on quality
    # If episodes are relevant (score >= 3.0), use them as primary
    # Otherwise, clear garbage episodes and use segments or fallback
    if not episodes_are_relevant:
        episodes = []  # always clear irrelevant episodes

    # These are mutually exclusive: if segments exist, use_fallback is False.
    # Priority: episodes (if relevant) > segments > fallback snippets.
    use_segments_as_primary = not episodes_are_relevant and bool(segments)
    use_fallback = not episodes_are_relevant and not segments and bool(fallback_snippets)

    # 9. Load artifacts for the top episode (only if episodes are the primary result)
    artifacts: dict[str, Any] = {}
    if episodes:
        artifacts = _load_episode_artifacts(store, episodes[0])

    # 10. Build narrative
    narrative = build_narrative(
        query=query,
        project_matches=project_matches,
        episodes=episodes,
        artifacts=artifacts,
        time_window=(since, until),
        segments=segments if use_segments_as_primary else [],
        fallback_snippets=fallback_snippets if use_fallback else [],
    )

    return RecallResult(
        query=query,
        project_matches=project_matches,
        time_window=(since, until),
        episodes=episodes,
        segments=segments,
        artifacts=artifacts,
        narrative=narrative,
    )


def recall_project_status(
    store: LonghandStore,
    project_name_or_id: str,
    max_commits: int = 10,
    max_episodes: int = 5,
    max_segments: int = 5,
) -> ProjectStatus | None:
    """Git-aware project status recall.

    Resolves a project name, queries git operations across all its sessions,
    links commits to episodes, and builds a "here's where you left off" narrative.
    Works without git — degrades gracefully to sessions/episodes/segments only.
    """
    from longhand.recall.narrative import build_project_status_narrative

    # 1. Resolve project — try direct ID first, then fuzzy match
    project = None
    try:
        direct = store.sqlite.get_project(project_name_or_id)
        if direct:
            project = direct
    except Exception:
        pass

    if not project:
        matches = match_projects(store, project_name_or_id, top_k=1)
        if not matches or matches[0].score < 0.5:
            return None
        match = matches[0]
        # Load full project row from SQLite
        try:
            project = store.sqlite.get_project(match.project_id)
        except Exception:
            pass
        if not project:
            # Use match data directly
            project = {
                "project_id": match.project_id,
                "display_name": match.display_name,
                "canonical_path": match.canonical_path,
                "category": match.category,
            }

    project_id = project["project_id"]

    # 2. Get recent git operations for this project
    try:
        last_commits = store.sqlite.get_project_git_operations(
            project_id=project_id, limit=max_commits,
        )
    except Exception:
        last_commits = []

    # Extract active branch from most recent git operation
    active_branch = None
    if last_commits:
        active_branch = last_commits[0].get("branch")

    # 3. Get recent sessions
    try:
        recent_sessions = store.sqlite.list_sessions(
            project_id=project_id, limit=5,
        )
    except Exception:
        recent_sessions = []

    # Get last outcome from most recent session
    last_outcome = None
    if recent_sessions:
        try:
            last_outcome = store.sqlite.get_outcome(recent_sessions[0]["session_id"])
        except Exception:
            pass

    # 4. Get recent episodes — split resolved vs unresolved
    try:
        all_episodes = store.sqlite.query_episodes(
            project_ids=[project_id], limit=max_episodes * 2,
        )
    except Exception:
        all_episodes = []

    recent_episodes = all_episodes[:max_episodes]
    unresolved_episodes = [
        ep for ep in all_episodes if ep.get("status") != "resolved"
    ][:max_episodes]

    # 5. Get recent conversation segments
    try:
        recent_segments = store.sqlite.query_segments(
            project_ids=[project_id], limit=max_segments,
        )
    except Exception:
        recent_segments = []

    # 6. Link commits to episodes via fix_commit_hash
    commit_hashes = {c.get("commit_hash") for c in last_commits if c.get("commit_hash")}
    episode_by_hash: dict[str, dict[str, Any]] = {}
    for ep in all_episodes:
        fch = ep.get("fix_commit_hash")
        if fch and fch in commit_hashes:
            episode_by_hash[fch] = ep

    for commit in last_commits:
        ch = commit.get("commit_hash")
        if ch and ch in episode_by_hash:
            commit["linked_episode"] = {
                "episode_id": episode_by_hash[ch].get("episode_id"),
                "fix_summary": episode_by_hash[ch].get("fix_summary", ""),
                "problem_description": episode_by_hash[ch].get("problem_description", ""),
            }

    # 7. Build narrative
    narrative = build_project_status_narrative(
        display_name=project.get("display_name", "unknown"),
        canonical_path=project.get("canonical_path", ""),
        last_commits=last_commits,
        active_branch=active_branch,
        recent_sessions=recent_sessions,
        recent_episodes=recent_episodes,
        unresolved_episodes=unresolved_episodes,
        recent_segments=recent_segments,
        last_outcome=last_outcome,
    )

    return ProjectStatus(
        project_id=project_id,
        display_name=project.get("display_name", "unknown"),
        canonical_path=project.get("canonical_path", ""),
        category=project.get("category"),
        last_commits=last_commits,
        active_branch=active_branch,
        recent_sessions=recent_sessions,
        recent_episodes=recent_episodes,
        unresolved_episodes=unresolved_episodes,
        recent_segments=recent_segments,
        last_outcome=last_outcome,
        narrative=narrative,
    )
