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

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from longhand.parser import discover_sessions
from longhand.recall.episode_search import find_episodes
from longhand.recall.narrative import build_narrative
from longhand.recall.project_match import ProjectMatch, match_projects
from longhand.recall.segment_search import find_segments
from longhand.recall.time_parser import parse_time_phrase
from longhand.replay import ReplayEngine
from longhand.storage.store import LonghandStore

# Keyword extraction for episode rank scoring. Stopwords are time/question
# fillers that don't help disambiguate; they would otherwise dominate scoring
# on queries like "what did we do last week with X".
_KEYWORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9]{3,}")
_KEYWORD_STOPWORDS = frozenset({
    "when", "what", "that", "this", "with", "from", "where",
    "made", "done", "last", "couple", "months", "weeks", "days",
    "time", "some", "thing", "about",
})


def _extract_query_keywords(query: str) -> list[str]:
    """Pull distinctive words from a query for keyword-hit rank scoring.

    Returns words ≥4 chars, lowercased, sorted by length (longest first),
    minus stopwords. Length-sorted because longer words are usually more
    distinctive than shorter ones for the substring-match scoring path.
    """
    return sorted(
        [w for w in _KEYWORD_RE.findall(query) if w.lower() not in _KEYWORD_STOPWORDS],
        key=len,
        reverse=True,
    )


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
    # Drift detection — disk vs sessions table
    session_count_indexed: int = 0
    session_count_on_disk: int = 0
    last_ingested_at_iso: str | None = None
    last_transcript_mtime_iso: str | None = None
    stale: bool = False
    stale_reason: str | None = None


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

    # 0. Transparent first-run backfill — if we're on a fresh upgrade and the
    # episodes vector collection is empty while the SQLite episode table is
    # populated, embed everything now so semantic episode search works.
    # Idempotent and a no-op after the first call.
    try:
        store.ensure_episode_embeddings()
    except Exception:
        pass

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

    query_words = _extract_query_keywords(cleaned_query)
    # 3. First attempt: semantic search with project + time filters.
    # find_episodes embeds the query against the episodes collection; keyword
    # stays None here so we don't over-filter on literal substrings.
    episodes = find_episodes(
        store=store,
        query=cleaned_query,
        project_ids=project_ids,
        since=since_iso,
        until=until_iso,
        has_fix=True,
        limit=max_episodes * 4,
    )

    # 4. Relax: drop project filter (the query may not be project-scoped)
    if not episodes:
        episodes = find_episodes(
            store=store,
            query=cleaned_query,
            project_ids=None,
            since=since_iso,
            until=until_iso,
            has_fix=True,
            limit=max_episodes * 4,
        )

    # 5. Relax: drop has_fix — accept unresolved problems too
    if not episodes:
        episodes = find_episodes(
            store=store,
            query=cleaned_query,
            project_ids=None,
            since=since_iso,
            until=until_iso,
            has_fix=False,
            limit=max_episodes * 4,
        )

    def _rank_score(ep: dict[str, Any]) -> float:
        confidence = ep.get("confidence") or 0.5

        # Keyword hit count — substring match on problem/diagnosis/fix text.
        # Literal matches are a strong relevance signal when the semantic
        # model already agrees.
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

        # Semantic boost from the episodes collection — `_distance` is
        # attached by find_episodes when the episode came from vector search.
        # Closer distance = stronger signal. A distance of 0.25 contributes
        # 15+ points, enough to clear the quality gate without keyword hits.
        ep_distance = ep.get("_distance")
        episode_semantic_boost = 0.0
        if ep_distance is not None:
            episode_semantic_boost = max(0.0, (1.0 - ep_distance) * 20)

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

        return (
            episode_semantic_boost
            + keyword_hits * 10
            + confidence
            + recency_boost * 0.5
        )

    episodes = sorted(episodes, key=_rank_score, reverse=True)[:max_episodes]

    # 7.5. Episode quality gate — reject clearly-irrelevant results.
    #
    # The old gate (score >= 15.0) was calibrated for keyword-hit scoring
    # (each hit = 10 points). For semantic-only hits (no keyword overlap)
    # even a strong match (distance 0.5 = 10 points of semantic_boost)
    # couldn't clear it, and episodes got silently dropped.
    #
    # New logic: if episodes came from semantic search (have _distance),
    # gate on distance directly — trust the vector collection's own
    # relevance ranking. Otherwise fall back to the old score gate for
    # keyword-path results.
    episodes_are_relevant = False
    if episodes:
        top = episodes[0]
        top_distance = top.get("_distance")
        if top_distance is not None:
            # Semantic-path gate — tuned empirically against ChromaDB's
            # default MiniLM-L6 embeddings, which cluster loosely (1.5+
            # for unrelated pairs, 0.7-1.2 for semantically related
            # pairs with little literal overlap). 1.5 is "at least
            # weakly related"; above that is near-random.
            #
            # Matches how segments are surfaced — they have no gate and
            # appear even at similar distances; there's no reason to
            # hold episodes to a stricter bar than segments.
            episodes_are_relevant = top_distance < 1.5
        else:
            # Keyword-only path (no semantic hit) — retain old 15.0 gate
            episodes_are_relevant = _rank_score(top) >= 15.0

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

    # 8.5. Secondary matches — when episodes win the primary slot, segments
    # from OTHER sessions used to be silently dropped. Surface them as a
    # footer so the user can see "I also have weaker hits in these sessions"
    # instead of recall going quiet on partial matches. Filter is by
    # session_id, with a relevance floor so only meaningful hits show up.
    secondary_segments: list[dict[str, Any]] = []
    if episodes and segments:
        episode_session_ids = {ep.get("session_id") for ep in episodes}
        for seg in segments:
            if seg.get("session_id") in episode_session_ids:
                continue
            if seg.get("_distance", 1.0) >= 1.5:
                continue
            secondary_segments.append(seg)

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
        secondary_segments=secondary_segments,
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

    # 7. Detect disk↔DB drift (staleness) for this project
    drift = _detect_project_drift(
        store, project_id, project.get("canonical_path", "")
    )

    # 8. Build narrative — prepend drift hint when stale so agents see it first
    latest_fix_summary = next(
        (
            (ep.get("fix_summary") or "").strip()
            for ep in recent_episodes
            if (ep.get("fix_summary") or "").strip()
        ),
        None,
    )
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
        latest_fix_summary=latest_fix_summary,
    )
    if drift["stale"] and drift["stale_reason"]:
        narrative = f"⚠ {drift['stale_reason']}\n\n{narrative}"

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
        session_count_indexed=drift["session_count_indexed"],
        session_count_on_disk=drift["session_count_on_disk"],
        last_ingested_at_iso=drift["last_ingested_at_iso"],
        last_transcript_mtime_iso=drift["last_transcript_mtime_iso"],
        stale=drift["stale"],
        stale_reason=drift["stale_reason"],
    )


def _detect_project_drift(
    store: LonghandStore,
    project_id: str,
    canonical_path: str,
) -> dict[str, Any]:
    """Check whether this project has transcripts on disk that aren't in the DB.

    Returns a dict with count/time fields and a `stale` flag. Stale is True only
    when there are un-ingested JSONLs referencing this project — sessions
    attributed to sibling projects don't trigger the flag (that's expected for
    multi-project sessions).

    Backed by a filesystem cache keyed on (transcript_path, mtime), so repeated
    calls are fast. The first call on a cold cache still walks every JSONL.
    """
    from datetime import datetime as _dt

    from longhand.recall.drift_cache import DriftCache, default_cache_path

    # 1. Transcripts indexed for THIS project + the full set of ingested paths.
    try:
        with store.sqlite.connect() as conn:
            indexed_rows = conn.execute(
                "SELECT transcript_path, ingested_at FROM sessions WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            all_ingested = {
                r[0]
                for r in conn.execute(
                    "SELECT transcript_path FROM sessions"
                ).fetchall()
            }
    except Exception:
        indexed_rows = []
        all_ingested = set()

    indexed_for_this_project = {r[0] for r in indexed_rows}
    session_count_indexed = len(indexed_for_this_project)

    last_ingested_at_iso: str | None = None
    if indexed_rows:
        last_ingested_at_iso = max(r[1] for r in indexed_rows if r[1])

    # 2. Classify on-disk JSONLs that reference this project but aren't
    #    attributed to it.
    target_resolved: Path | None
    try:
        target_resolved = (
            Path(canonical_path).resolve() if canonical_path else None
        )
    except (OSError, PermissionError):
        target_resolved = None

    truly_missing: list[Path] = []       # not in any sessions row
    cross_attributed: list[Path] = []    # ingested but mapped to a sibling
    max_mtime: float = 0.0

    if canonical_path:
        cache = DriftCache(default_cache_path(store.data_dir))
        on_disk = discover_sessions()
        valid_paths = {str(p) for p in on_disk}
        cache.prune(valid_paths)

        for jsonl in on_disk:
            jsonl_str = str(jsonl)
            if jsonl_str in indexed_for_this_project:
                continue
            entry = cache.get_or_compute(jsonl)
            if entry is None or not entry.references(canonical_path, target_resolved):
                continue

            if jsonl_str in all_ingested:
                cross_attributed.append(jsonl)
            else:
                truly_missing.append(jsonl)
                try:
                    mtime = jsonl.stat().st_mtime
                    if mtime > max_mtime:
                        max_mtime = mtime
                except (OSError, PermissionError):
                    pass

        try:
            cache.save()
        except OSError:
            # Cache is best-effort; a failed write shouldn't break recall.
            pass

    # On-disk total for this project = truly-ours + truly-missing-that-reference.
    # Cross-attributed sessions aren't counted here — they belong to siblings.
    session_count_on_disk = session_count_indexed + len(truly_missing)

    last_transcript_mtime_iso: str | None = None
    if max_mtime > 0.0:
        last_transcript_mtime_iso = _dt.fromtimestamp(max_mtime).isoformat()

    stale = len(truly_missing) > 0
    stale_reason: str | None = None
    if stale:
        n = len(truly_missing)
        plural = "session" if n == 1 else "sessions"
        stale_reason = (
            f"{n} {plural} on disk not yet indexed for this project — "
            f"run `longhand reconcile --fix` to catch up"
        )

    return {
        "session_count_indexed": session_count_indexed,
        "session_count_on_disk": session_count_on_disk,
        "last_ingested_at_iso": last_ingested_at_iso,
        "last_transcript_mtime_iso": last_transcript_mtime_iso,
        "stale": stale,
        "stale_reason": stale_reason,
    }
