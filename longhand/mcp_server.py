"""
Longhand MCP server — lets Claude query Longhand memory during live sessions.

Implements the Model Context Protocol so Claude Desktop and Claude Code
can search, retrieve, and replay session data as tool calls.

Run with:
    python -m longhand.mcp_server

The `mcp` dependency ships with Longhand; no extra install needed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    print(
        "The `mcp` package is required for the MCP server. "
        "It ships with Longhand — reinstall with:\n"
        "    pip install --upgrade longhand",
        file=sys.stderr,
    )
    sys.exit(1)

from longhand.cli.helpers import _resolve_prefix
from longhand.recall import recall as recall_pipeline
from longhand.recall.project_match import match_projects
from longhand.recall.recall_pipeline import recall_project_status, staleness_banner
from longhand.recall.reconcile import run_reconcile
from longhand.replay import ReplayEngine
from longhand.storage import LonghandStore
from longhand.storage.sqlite_store import _escape_like

server: Server = Server("longhand")


def _format_event(row: dict[str, Any], content_chars: int = 1500) -> dict[str, Any]:
    """Turn a raw SQLite event row into a compact dict for Claude."""
    return {
        "event_id": row["event_id"],
        "session_id": row["session_id"],
        "event_type": row["event_type"],
        "timestamp": row["timestamp"],
        "tool_name": row.get("tool_name"),
        "file_path": row.get("file_path"),
        "content": (row.get("content") or "")[:content_chars],
    }


def _truncate_output(text: str, max_chars: int, hint: str | None = None) -> str:
    """Cap output size and append a tool-specific pagination hint if truncated.

    `hint` names the parameters the emitting tool actually accepts — callers
    pass their own string so the suggestion matches the tool's surface.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    hint_text = hint or "narrow your query"
    return text[:max_chars] + (
        f"\n\n[... truncated at {max_chars} chars. {hint_text}]"
    )


# Tool-specific pagination hints — each names the params that actually exist
# on the tool's surface so the user/agent can follow the hint literally.
_HINT_SEARCH = "Use a smaller `limit` or add filters (session_id, tool_name, file_path_contains)."
_HINT_SEARCH_IN_CONTEXT = "Use a smaller `limit`, fewer `context_events`, or a narrower query."
_HINT_LIST_SESSIONS = "Use a smaller `limit` or a `project` filter."
_HINT_TIMELINE = "Use `offset`/`limit`, `tail` for the last N events, or `summary_only: true`."
_HINT_LATEST = "Use a smaller `limit` or filter by `event_type`."
_HINT_RECALL = "Use a smaller `max_episodes` or a more specific query."
_HINT_GIT = "Use a smaller `limit` or filter by `operation_type`."
_HINT_PROJECTS = "Use a smaller `limit`, a `keyword`, or a `category` filter."


MAX_LIMIT = 1000
MAX_OUTPUT_CHARS = 200000


def _int(value: Any, default: int) -> int:
    """Coerce a value to int, handling string inputs from MCP bridge."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _limit(value: Any, default: int) -> int:
    """Coerce to int and cap at MAX_LIMIT to prevent OOM on huge result sets."""
    return min(_int(value, default), MAX_LIMIT)


def _max_chars(value: Any, default: int) -> int:
    """Coerce to int and cap at MAX_OUTPUT_CHARS."""
    return min(_int(value, default), MAX_OUTPUT_CHARS)


def _bool(value: Any, default: bool) -> bool:
    """Coerce a value to bool, handling string inputs from MCP bridge."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _format_project_compact(row: dict[str, Any]) -> dict[str, Any]:
    """Return a compact project summary (no JSON blobs)."""
    return {
        "project_id": row["project_id"],
        "display_name": row.get("display_name"),
        "canonical_path": row.get("canonical_path"),
        "category": row.get("category"),
        "session_count": row.get("session_count"),
        "total_edits": row.get("total_edits"),
        "last_seen": row.get("last_seen"),
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description=(
                "Semantic search across all stored Claude Code session events. "
                "Returns events matching a natural language query, with optional "
                "filters by event type, session, project, tool, or file path. "
                "IMPORTANT: Always pass session_id when you know which session to search — "
                "unscoped search returns noisy results. For finding a discussion WITH "
                "surrounding conversation context, use search_in_context instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query"},
                    "limit": {"default": 10, "description": "Max results (default 10)"},
                    "session_id": {"type": "string", "description": "Scope search to a single session (prefix match)"},
                    "project_id": {"type": "string", "description": "Scope search to a project by project_id"},
                    "project_name": {"type": "string", "description": "Scope search to a project by name substring (e.g. 'gonzo')"},
                    "event_type": {
                        "type": "string",
                        "description": "Filter: user_message, assistant_text, assistant_thinking, tool_call, tool_result",
                    },
                    "tool_name": {"type": "string", "description": "Filter by tool name (Edit, Bash, Read, etc.)"},
                    "file_path_contains": {"type": "string", "description": "Filter to events with an explicit file_path containing this string (tool_call/tool_result events only — user messages won't have file_path metadata)"},
                    "max_chars": {"default": 12000, "description": "Max total output characters (default 12000). Set higher if you need full content."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="search_in_context",
            description=(
                "Search within a specific session and return matches WITH surrounding "
                "conversation context. This is the tool you want when you know WHICH "
                "session to look in but need to FIND a specific discussion or event. "
                "Returns each semantic match plus N events before/after it from the "
                "timeline, so you can read the full conversation flow. "
                "Much more efficient than paginating get_session_timeline manually."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (prefix match)"},
                    "query": {"type": "string", "description": "Natural language query to find within the session"},
                    "context_events": {"default": 5, "description": "Number of events to include before AND after each match (default 5)"},
                    "limit": {"default": 3, "description": "Max number of matches to return with context (default 3)"},
                    "event_type": {"type": "string", "description": "Optional: filter matches to a single event type"},
                    "max_chars": {"default": 20000, "description": "Max total output characters (default 20000)"},
                },
                "required": ["session_id", "query"],
            },
        ),
        Tool(
            name="list_sessions",
            description=(
                "List recent Claude Code sessions that Longhand has indexed. "
                "Returns session ID, project path, start/end timestamps, event count, "
                "and outcome (shipped/fixed/stuck/exploratory) for each session. "
                "Use this to orient before drilling into a specific session — e.g., "
                "'which sessions touched this project last week?' Filter by project "
                "path substring to narrow results. NOT for searching content — "
                "use search or recall for that. When a `project` filter matches a "
                "known project and its on-disk transcripts exceed what's indexed, "
                "the response wraps a `{stale, stale_reason, sessions}` envelope — "
                "call the `reconcile` tool to catch up."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Filter by project path substring (e.g. 'longhand', 'bsoi')"},
                    "limit": {"default": 50, "description": "Max results (default 50)"},
                },
            },
        ),
        Tool(
            name="get_session_timeline",
            description=(
                "Get a chronological timeline of events in a session. Supports session "
                "id prefix match. Use 'tail' to get only the last N events (great for "
                "checking how a session ended). Use 'offset' to paginate through long sessions. "
                "NOT for searching — if you're looking for something specific in a session, "
                "use search_in_context instead of paginating this tool in a loop."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"default": 100, "description": "Max events to return (default 100)"},
                    "offset": {"default": 0, "description": "Skip first N events (for pagination)"},
                    "tail": {"description": "Return only the last N events of the session"},
                    "include_thinking": {"type": "boolean", "default": True},
                    "event_type": {"type": "string", "description": "Filter to a single event type"},
                    "summary_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return only event_type, timestamp, tool_name, file_path — no content. Great for scanning long sessions.",
                    },
                    "max_chars": {"default": 16000, "description": "Max total output characters"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="get_latest_events",
            description=(
                "Get the N most recent events in a session, in reverse chronological order "
                "(sequence DESC). Use this when you need 'what was the latest X' — e.g., "
                "the last user message, the last tool call, the last assistant response. "
                "Semantic search is the wrong tool for recency; this one is. Supports "
                "session id prefix match."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"default": 10, "description": "Max events to return (default 10)"},
                    "event_type": {
                        "type": "string",
                        "description": "Optional: filter to a single event type (user_message, assistant_text, tool_call, etc.)",
                    },
                    "max_chars": {"default": 16000, "description": "Max total output characters"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="replay_file",
            description=(
                "Reconstruct the exact state of a file at any point in a past session. "
                "Applies every Write/Edit event verbatim from the session JSONL in order — "
                "no summarization, no approximation. Returns the full file content as it "
                "existed at that moment. Use this when you need to see 'what did this file "
                "look like after that refactor?' or 'what was the state before it broke?' "
                "Pass at_event_id to stop replay at a specific event; omit it to get the "
                "final state at session end. Requires session_id and file_path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (prefix match supported)"},
                    "file_path": {"type": "string", "description": "Exact file path to reconstruct"},
                    "at_event_id": {"type": "string", "description": "Optional: stop replay at this event ID to get mid-session state"},
                },
                "required": ["session_id", "file_path"],
            },
        ),
        Tool(
            name="get_file_history",
            description=(
                "Get every edit ever made to a file across all sessions, in chronological "
                "order. Returns session ID, timestamp, event type (Write/Edit), old and new "
                "content for each change. Use this to answer 'how has this file evolved?' or "
                "'who changed this and when?' across your entire history. Optionally scope "
                "to a single session. For reconstructing exact file state at a point in time, "
                "use replay_file instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Exact file path to look up"},
                    "session_id": {"type": "string", "description": "Optional: limit to a single session (prefix match)"},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_stats",
            description=(
                "Get overall Longhand storage statistics: total sessions, events, "
                "tool calls, file edits, thinking blocks, vectors indexed, projects, "
                "episodes (resolved/unresolved), and data directory path. "
                "Use this to verify Longhand is healthy, check how much history is "
                "indexed, or answer 'how many sessions do I have?' Returns a flat "
                "key-value object — no parameters needed."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="recall",
            description=(
                "PROACTIVE MEMORY — START HERE for any 'do you remember...' question. "
                "Handles fuzzy time references ('a couple months ago'), project matching "
                "('that game project'), and episode retrieval in ONE call. Returns: matched "
                "projects, relevant episodes (problem→fix pairs), diffs, verbatim thinking "
                "blocks, reconstructed file states, and a prebuilt markdown narrative. "
                "Do NOT manually search and paginate — use this tool first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language question"},
                    "max_episodes": {"default": 5, "description": "Max episodes to return"},
                    "max_chars": {"default": 16000, "description": "Max total output characters"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="recall_project_status",
            description=(
                "Get the current status of a project — where you left off, recent "
                "commits, unresolved issues, and latest conversation context. Takes "
                "a project name (fuzzy match) and returns a structured summary with "
                "git history, linked episodes, and conversation segments. "
                "Use this when someone says 'pick up where we left off on X', "
                "'what's the status of X', or 'where did we end on X'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name, alias, or ID (fuzzy match)",
                    },
                    "max_commits": {"default": 10, "description": "Max recent commits to show"},
                    "max_episodes": {"default": 5, "description": "Max recent episodes"},
                    "max_chars": {"default": 16000, "description": "Max output characters"},
                },
                "required": ["project"],
            },
        ),
        Tool(
            name="reconcile",
            description=(
                "Bridge disk↔DB drift from inside an MCP session. Re-ingests any "
                "on-disk Claude Code transcripts that are missing from the sessions "
                "table or have NULL project_id, using current project-inference "
                "logic. Call this when another tool surfaces `stale: true` with a "
                "`stale_reason` pointing at reconcile, or when `recall_project_status` "
                "reports `session_count_on_disk > session_count_indexed`. "
                "Returns counts of fully-indexed, null-project, missing, ingested, "
                "and errors. Acquires the ingest lock — serialized with concurrent "
                "ingestion. May take 30s+ on cold state because it runs embeddings "
                "and episode extraction during re-ingest."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "fix": {
                        "type": "boolean",
                        "default": True,
                        "description": "Re-ingest missing/null-project transcripts (default True). Pass false for a dry-run summary.",
                    },
                },
            },
        ),
        Tool(
            name="match_project",
            description=(
                "Fuzzy project matching. Given a partial project name, category, or "
                "description, returns candidate projects with match reasons. Useful for "
                "confirming 'which game did you mean?' before drilling into episodes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"default": 5, "description": "Max project matches to return"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="find_episodes",
            description=(
                "Structured search for problem→fix episodes. Filters: project_ids, time "
                "range, keyword, has_fix. Returns raw episode rows. Use this when you "
                "already know the project or want data instead of narrative."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_ids": {"type": "array", "items": {"type": "string"}},
                    "since": {"type": "string", "description": "ISO timestamp"},
                    "until": {"type": "string", "description": "ISO timestamp"},
                    "keyword": {"type": "string"},
                    "has_fix": {"type": "boolean", "default": True},
                    "limit": {"default": 20, "description": "Max results (default 20)"},
                },
            },
        ),
        Tool(
            name="get_episode",
            description=(
                "Full detail for one episode by episode_id. Includes all referenced events "
                "(problem, diagnosis thinking block, fix edit, verification), the diff, "
                "and the reconstructed file state after the fix."
            ),
            inputSchema={
                "type": "object",
                "properties": {"episode_id": {"type": "string"}},
                "required": ["episode_id"],
            },
        ),
        Tool(
            name="get_session_commits",
            description=(
                "Get all git operations (commits, pushes, merges, checkouts, etc.) from a "
                "session, chronologically. Links session work to git history — the in-between "
                "that git log doesn't capture."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (prefix match)"},
                    "operation_type": {"type": "string", "description": "Filter: commit, push, pull, checkout, merge, etc."},
                    "limit": {"default": 100, "description": "Max results"},
                    "max_chars": {"default": 12000, "description": "Max output characters"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="find_commits",
            description=(
                "Search across all sessions for git commits matching a query — by commit "
                "message, hash prefix, or branch name. Great for 'find that commit where "
                "we fixed the parser' queries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Commit message substring, hash prefix, or branch name"},
                    "session_id": {"type": "string", "description": "Optional: scope to a single session (prefix match)"},
                    "operation_type": {"type": "string", "description": "Filter by operation type (default: all)"},
                    "limit": {"default": 20, "description": "Max results"},
                    "max_chars": {"default": 12000, "description": "Max output characters"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_projects",
            description=(
                "Browse all projects Longhand has inferred from your session history. "
                "Returns project ID, display name, canonical path, category (cli tool / "
                "web app / library / etc.), and session count. Filter by keyword (matches "
                "name, path, aliases) or category. Use this to find a project_id before "
                "calling get_project_timeline or recall_project_status. Set verbose=true "
                "to include full metadata (aliases, languages, keywords arrays)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Filter: matches project name, path, or aliases"},
                    "category": {"type": "string", "description": "Filter: e.g. 'cli tool', 'web app', 'library'"},
                    "limit": {"default": 20, "description": "Max results (default 20)"},
                    "verbose": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return full project rows including aliases, keywords, languages JSON",
                    },
                },
            },
        ),
        Tool(
            name="get_project_timeline",
            description=(
                "Session-level timeline for a project — bird's-eye view of what's been "
                "happening. Returns each session's start/end time, event count, outcome "
                "(shipped / fixed / stuck / exploratory), and a summary line. Use this "
                "after list_projects to understand velocity and recent activity on a "
                "specific project. Filter by date range with since/until (ISO format). "
                "For content-level search within a project, use search with project_name "
                "instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "Project ID from list_projects or match_project"},
                    "since": {"type": "string", "description": "ISO date — only sessions after this date"},
                    "until": {"type": "string", "description": "ISO date — only sessions before this date"},
                    "limit": {"default": 50, "description": "Max results (default 50)"},
                },
                "required": ["project_id"],
            },
        ),
    ]


# ─── Tool handlers ──────────────────────────────────────────────────────────
#
# Each tool is a module-level async function taking (store, arguments) and
# returning a list[TextContent]. This makes them individually unit-testable
# without going through the MCP dispatch layer.


async def _tool_search(store: LonghandStore, arguments: dict[str, Any]) -> list[TextContent]:
    limit = _limit(arguments.get("limit"), 10)
    max_chars = _max_chars(arguments.get("max_chars"), 12000)

    # Resolve session_id prefix if provided
    search_session_id = None
    if arguments.get("session_id"):
        search_session_id = _resolve_prefix(store, arguments["session_id"])

    # Resolve project_name → set of session_ids for post-filtering
    project_session_ids: set[str] | None = None
    project_id = arguments.get("project_id")
    project_name = arguments.get("project_name")

    # Auto-scope when the query names a project but no explicit project
    # filter was set. Reuses match_projects — the same fuzzy infrastructure
    # the recall tool uses — so literal project names don't get buried under
    # semantically-similar events from sibling projects.
    auto_scoped_to: str | None = None
    if not project_id and not project_name:
        try:
            matches = match_projects(store, arguments.get("query", ""), top_k=1)
            if matches and matches[0].score >= 0.8:
                project_name = matches[0].display_name
                auto_scoped_to = matches[0].display_name
        except Exception:
            pass

    if project_id or project_name:
        if project_name and not project_id:
            projects = store.sqlite.list_projects(keyword=project_name, limit=5)
            if projects:
                project_id = projects[0]["project_id"]
        if project_id:
            proj_sessions = store.sqlite.list_sessions(project_id=project_id, limit=1000)
            project_session_ids = {s["session_id"] for s in proj_sessions}

            # Fallback: if no sessions linked via project_id, find sessions
            # that edited files in the project's directory
            if not project_session_ids:
                proj = store.sqlite.get_project(project_id)
                if proj and proj.get("canonical_path"):
                    canon = proj["canonical_path"]
                    with store.sqlite.connect() as conn:
                        rows = conn.execute(
                            "SELECT DISTINCT session_id FROM events "
                            "WHERE file_path LIKE ? ESCAPE '\\'",
                            (f"%{_escape_like(canon)}%",),
                        ).fetchall()
                        project_session_ids = {r["session_id"] for r in rows}

    # When post-filters are active, request extra results so we have enough after filtering
    has_post_filter = project_session_ids is not None or arguments.get("file_path_contains")
    fetch_multiplier = 5 if has_post_filter else 1

    hits = store.vectors.search(
        query=arguments["query"],
        n_results=limit * fetch_multiplier,
        event_type=arguments.get("event_type"),
        session_id=search_session_id,
        tool_name=arguments.get("tool_name"),
        file_path_contains=arguments.get("file_path_contains"),
    )

    # Post-filter by project if needed
    if project_session_ids is not None:
        hits = [
            h for h in hits
            if (h.get("metadata") or {}).get("session_id") in project_session_ids
        ]

    hits = hits[:limit]

    # Staleness banner: when this search was scoped to a project (auto or
    # explicit), check whether that project has on-disk transcripts not yet in
    # the DB. If so, wrap the response with a stale/stale_reason banner so the
    # caller sees the same signal `recall_project_status` surfaces — closes the
    # silent-failure class where a stale index returned `hits: []` with no hint.
    canonical_path: str | None = None
    if project_id:
        proj = store.sqlite.get_project(project_id)
        if proj:
            canonical_path = proj.get("canonical_path")
    banner = staleness_banner(store, project_id, canonical_path)

    payload: dict[str, Any] | list[dict[str, Any]]
    if auto_scoped_to is not None or banner is not None:
        envelope: dict[str, Any] = {}
        if auto_scoped_to is not None:
            envelope["auto_scoped_to"] = auto_scoped_to
            envelope["auto_scope_hint"] = (
                f"Query appeared to name a project; results are scoped to "
                f"'{auto_scoped_to}'. Pass project_name=None to override."
            )
        if banner is not None:
            envelope["stale"] = True
            envelope["stale_reason"] = banner["stale_reason"]
        envelope["hits"] = hits
        payload = envelope
    else:
        payload = hits
    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_SEARCH))]


async def _tool_search_in_context(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    full_id = _resolve_prefix(store, arguments["session_id"])
    if not full_id:
        return [TextContent(type="text", text=f"No session matching prefix: {arguments['session_id']}")]

    context_n = _int(arguments.get("context_events"), 5)
    limit = _limit(arguments.get("limit"), 3)
    max_chars = _max_chars(arguments.get("max_chars"), 20000)

    # Semantic search scoped to this session
    hits = store.vectors.search(
        query=arguments["query"],
        n_results=limit * 3,  # fetch extra for dedup
        session_id=full_id,
        event_type=arguments.get("event_type"),
    )

    if not hits:
        return [TextContent(type="text", text="No matches found in this session.")]

    # Build context windows from sequence numbers
    windows: list[dict[str, Any]] = []
    for hit in hits:
        seq = (hit.get("metadata") or {}).get("sequence")
        if seq is None:
            continue
        seq = int(seq)
        windows.append({
            "match_event_id": hit["event_id"],
            "match_distance": hit.get("distance", 1.0),
            "seq_start": max(0, seq - context_n),
            "seq_end": seq + context_n,
        })

    if not windows:
        return [TextContent(type="text", text="Matches found but no sequence metadata available.")]

    # Merge strictly overlapping windows. We used to merge on adjacency
    # (`<= seq_end + 1`) which collapsed unrelated matches separated by a
    # single sequence step — the resulting blob spanned conversations that
    # had nothing to do with each other. Strict overlap keeps each match's
    # context coherent.
    windows.sort(key=lambda w: w["seq_start"])
    merged: list[dict[str, Any]] = []
    for w in windows:
        if merged and w["seq_start"] <= merged[-1]["seq_end"]:
            merged[-1]["seq_end"] = max(merged[-1]["seq_end"], w["seq_end"])
            merged[-1]["match_event_ids"].append(w["match_event_id"])
        else:
            merged.append({
                "seq_start": w["seq_start"],
                "seq_end": w["seq_end"],
                "match_event_ids": [w["match_event_id"]],
            })

    # Deduplicate: keep only the first `limit` unique matches
    all_match_ids: list[str] = []
    for mw in merged:
        for mid in mw["match_event_ids"]:
            if mid not in all_match_ids:
                all_match_ids.append(mid)
    all_match_ids = all_match_ids[:limit]
    match_id_set = set(all_match_ids)

    # Re-filter merged windows to only those containing kept matches
    final_windows: list[dict[str, Any]] = []
    for mw in merged:
        kept = [m for m in mw["match_event_ids"] if m in match_id_set]
        if kept:
            mw["match_event_ids"] = kept
            final_windows.append(mw)

    # Fetch events for each window
    results = []
    for mw in final_windows:
        events = store.sqlite.get_events_by_sequence_range(
            full_id, mw["seq_start"], mw["seq_end"]
        )
        formatted = []
        for e in events:
            fe = _format_event(e)
            fe["is_match"] = e["event_id"] in match_id_set
            formatted.append(fe)
        results.append({
            "sequence_range": [mw["seq_start"], mw["seq_end"]],
            "events": formatted,
        })

    payload = {
        "session_id": full_id,
        "query": arguments["query"],
        "total_matches": len(hits),
        "showing": len(all_match_ids),
        "context_windows": results,
    }
    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_SEARCH_IN_CONTEXT))]


async def _tool_list_sessions(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    project_filter = arguments.get("project")
    rows = store.sqlite.list_sessions(
        project_path=project_filter,
        limit=_limit(arguments.get("limit"), 50),
    )

    # Staleness banner: when filtered by project, resolve to a project_id and
    # surface drift. Global list_sessions (no filter) has no project to check.
    banner = None
    if project_filter:
        try:
            matches = match_projects(store, project_filter, top_k=1)
            if matches and matches[0].score >= 0.8:
                proj = store.sqlite.get_project(matches[0].project_id)
                if proj:
                    banner = staleness_banner(
                        store,
                        matches[0].project_id,
                        proj.get("canonical_path"),
                    )
        except Exception:
            banner = None

    payload: dict[str, Any] | list[dict[str, Any]]
    if banner is not None:
        payload = {
            "stale": True,
            "stale_reason": banner["stale_reason"],
            "sessions": rows,
        }
    else:
        payload = rows
    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, 16000, _HINT_LIST_SESSIONS))]


async def _tool_get_session_timeline(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    full_id = _resolve_prefix(store, arguments["session_id"])
    if not full_id:
        return [TextContent(type="text", text=f"No session matching: {arguments['session_id']}")]

    tail = _limit(arguments.get("tail"), 0)
    offset = _int(arguments.get("offset"), 0)
    limit = _limit(arguments.get("limit"), 100)
    max_chars = _max_chars(arguments.get("max_chars"), 16000)
    include_thinking = _bool(arguments.get("include_thinking"), True)
    summary_only = _bool(arguments.get("summary_only"), False)

    if tail:
        # For tail: fetch all events (up to a reasonable cap) then slice the end
        all_events = store.sqlite.get_events(
            session_id=full_id,
            event_type=arguments.get("event_type"),
            limit=5000,
        )
        if not include_thinking:
            all_events = [e for e in all_events if e["event_type"] != "assistant_thinking"]
        # Filter out epoch-timestamp unknown events by default
        all_events = [e for e in all_events if not e["event_type"].startswith("unk")]
        events = all_events[-tail:]
    else:
        events = store.sqlite.get_events(
            session_id=full_id,
            event_type=arguments.get("event_type"),
            limit=limit,
            offset=offset,
        )
        if not include_thinking:
            events = [e for e in events if e["event_type"] != "assistant_thinking"]
        # Filter out epoch-timestamp unknown events by default
        events = [e for e in events if not e["event_type"].startswith("unk")]

    if summary_only:
        formatted = [
            {
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "timestamp": e["timestamp"],
                "tool_name": e.get("tool_name"),
                "file_path": e.get("file_path"),
            }
            for e in events
        ]
    else:
        formatted = [_format_event(e) for e in events]

    # Add pagination metadata
    meta: dict[str, Any] = {
        "session_id": full_id,
        "returned": len(formatted),
        "offset": offset,
    }
    if tail:
        meta["tail"] = tail
    payload = {"meta": meta, "events": formatted}

    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_TIMELINE))]


async def _tool_get_latest_events(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    full_id = _resolve_prefix(store, arguments["session_id"])
    if not full_id:
        return [TextContent(type="text", text=f"No session matching: {arguments['session_id']}")]

    limit = _limit(arguments.get("limit"), 10)
    max_chars = _max_chars(arguments.get("max_chars"), 16000)
    event_type = arguments.get("event_type")

    events = store.sqlite.get_latest_events(
        session_id=full_id,
        limit=limit,
        event_type=event_type,
    )

    formatted = [_format_event(e) for e in events]
    payload = {
        "meta": {
            "session_id": full_id,
            "returned": len(formatted),
            "limit": limit,
            "event_type": event_type,
            "order": "sequence DESC",
        },
        "events": formatted,
    }
    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_LATEST))]


async def _tool_replay_file(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    full_id = _resolve_prefix(store, arguments["session_id"])
    if not full_id:
        return [TextContent(type="text", text=f"No session matching: {arguments['session_id']}")]

    engine = ReplayEngine(store.sqlite)
    state = engine.file_state_at(
        file_path=arguments["file_path"],
        session_id=full_id,
        at_event_id=arguments.get("at_event_id"),
    )
    if not state:
        return [TextContent(type="text", text=f"No edits found for {arguments['file_path']}")]

    return [TextContent(
        type="text",
        text=json.dumps({
            "file_path": state.file_path,
            "session_id": state.session_id,
            "at_event_id": state.at_event_id,
            "at_timestamp": state.at_timestamp.isoformat(),
            "source": state.source,
            "edits_applied": state.edits_applied,
            "content": state.content,
        }, indent=2, default=str),
    )]


async def _tool_get_file_history(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    engine = ReplayEngine(store.sqlite)
    full_session = None
    if arguments.get("session_id"):
        full_session = _resolve_prefix(store, arguments["session_id"])
    edits = engine.file_history(arguments["file_path"], session_id=full_session)
    formatted = [
        {
            "event_id": e["event_id"],
            "session_id": e["session_id"],
            "timestamp": e["timestamp"],
            "tool_name": e.get("tool_name"),
            "old_content": (e.get("old_content") or "")[:800],
            "new_content": (e.get("new_content") or "")[:800],
        }
        for e in edits
    ]
    return [TextContent(type="text", text=json.dumps(formatted, indent=2, default=str))]


async def _tool_get_stats(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    stats = store.stats()
    return [TextContent(type="text", text=json.dumps(stats, indent=2, default=str))]


# ─── Proactive memory tools (v0.2) ─────────────────────────────────────


async def _tool_recall(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    result = recall_pipeline(
        store=store,
        query=arguments["query"],
        max_episodes=_limit(arguments.get("max_episodes"), 5),
    )
    payload = {
        "query": result.query,
        "project_matches": [
            {
                "project_id": m.project_id,
                "display_name": m.display_name,
                "category": m.category,
                "canonical_path": m.canonical_path,
                "score": m.score,
                "reasons": m.reasons,
            }
            for m in result.project_matches
        ],
        "time_window": {
            "since": result.time_window[0].isoformat() if result.time_window[0] else None,
            "until": result.time_window[1].isoformat() if result.time_window[1] else None,
        },
        "episodes": result.episodes,
        "segments": result.segments,
        "narrative": result.narrative,
    }
    # Only include `artifacts` when populated — keeps the payload tight
    # and makes the key's presence meaningful (indicates a fix event +
    # linked file state were reconstructed for the top episode).
    if result.artifacts:
        payload["artifacts"] = result.artifacts
    max_chars = _max_chars(arguments.get("max_chars"), 16000)
    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_RECALL))]


async def _tool_recall_project_status(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    status = recall_project_status(
        store=store,
        project_name_or_id=arguments["project"],
        max_commits=_limit(arguments.get("max_commits"), 10),
        max_episodes=_limit(arguments.get("max_episodes"), 5),
    )
    if not status:
        return [TextContent(type="text", text="No project matched.")]
    payload = {
        "project_id": status.project_id,
        "display_name": status.display_name,
        "canonical_path": status.canonical_path,
        "category": status.category,
        "active_branch": status.active_branch,
        "last_commits": status.last_commits[:10],
        "recent_sessions": [
            {
                "session_id": s.get("session_id"),
                "started_at": s.get("started_at"),
                "ended_at": s.get("ended_at"),
                "event_count": s.get("event_count"),
            }
            for s in status.recent_sessions[:5]
        ],
        "recent_episodes": [
            {
                "episode_id": e.get("episode_id"),
                "problem_description": (e.get("problem_description") or "")[:120],
                "fix_summary": (e.get("fix_summary") or "")[:120],
                "status": e.get("status"),
                "ended_at": e.get("ended_at"),
            }
            for e in status.recent_episodes[:5]
        ],
        "unresolved_episodes": [
            {
                "episode_id": e.get("episode_id"),
                "problem_description": (e.get("problem_description") or "")[:120],
                "ended_at": e.get("ended_at"),
            }
            for e in status.unresolved_episodes[:5]
        ],
        "recent_segments": [
            {
                "segment_type": s.get("segment_type"),
                "topic": (s.get("topic") or "")[:100],
                "ended_at": s.get("ended_at"),
            }
            for s in status.recent_segments[:5]
        ],
        "last_outcome": status.last_outcome,
        "narrative": status.narrative,
        "session_count_indexed": status.session_count_indexed,
        "session_count_on_disk": status.session_count_on_disk,
        "last_ingested_at": status.last_ingested_at_iso,
        "last_transcript_mtime": status.last_transcript_mtime_iso,
        "stale": status.stale,
        "stale_reason": status.stale_reason,
    }
    max_chars = _max_chars(arguments.get("max_chars"), 16000)
    output = json.dumps(payload, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_RECALL))]


async def _tool_match_project(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    matches = match_projects(
        store=store,
        query=arguments["query"],
        top_k=_limit(arguments.get("top_k"), 5),
    )
    payload = [
        {
            "project_id": m.project_id,
            "display_name": m.display_name,
            "category": m.category,
            "canonical_path": m.canonical_path,
            "score": m.score,
            "reasons": m.reasons,
        }
        for m in matches
    ]
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def _tool_find_episodes(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    episodes = store.sqlite.query_episodes(
        project_ids=arguments.get("project_ids"),
        since=arguments.get("since"),
        until=arguments.get("until"),
        keyword=arguments.get("keyword"),
        limit=_limit(arguments.get("limit"), 20),
    )
    if _bool(arguments.get("has_fix"), True):
        episodes = [e for e in episodes if e.get("fix_event_id")]
    return [TextContent(type="text", text=json.dumps(episodes, indent=2, default=str))]


async def _tool_get_episode(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    ep = store.sqlite.get_episode(arguments["episode_id"])
    if not ep:
        return [TextContent(type="text", text=f"No episode: {arguments['episode_id']}")]

    # Load related events and artifacts
    payload: dict[str, Any] = {"episode": ep}
    for field, key in [
        ("problem_event_id", "problem_event"),
        ("diagnosis_event_id", "diagnosis_event"),
        ("fix_event_id", "fix_event"),
        ("verification_event_id", "verification_event"),
    ]:
        eid = ep.get(field)
        if eid:
            evt = store.sqlite.get_event(eid)
            if evt:
                payload[key] = _format_event(evt)

    # Reconstructed file state after fix
    fix_id = ep.get("fix_event_id")
    if fix_id:
        fix_event = store.sqlite.get_event(fix_id)
        if fix_event and fix_event.get("file_path"):
            engine = ReplayEngine(store.sqlite)
            try:
                state = engine.file_state_at(
                    file_path=fix_event["file_path"],
                    session_id=ep["session_id"],
                    at_event_id=fix_id,
                )
                if state:
                    payload["file_state_after"] = {
                        "file_path": state.file_path,
                        "content": state.content,
                        "edits_applied": state.edits_applied,
                    }
            except Exception:
                pass

    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def _tool_get_session_commits(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    full_id = _resolve_prefix(store, arguments["session_id"])
    if not full_id:
        return [TextContent(type="text", text=f"No session matching: {arguments['session_id']}")]
    ops = store.sqlite.get_git_operations(
        session_id=full_id,
        operation_type=arguments.get("operation_type"),
        limit=_limit(arguments.get("limit"), 100),
    )
    max_chars = _max_chars(arguments.get("max_chars"), 12000)
    output = json.dumps(ops, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_GIT))]


async def _tool_find_commits(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    search_session_id = None
    if arguments.get("session_id"):
        search_session_id = _resolve_prefix(store, arguments["session_id"])
    ops = store.sqlite.search_git_operations(
        query=arguments["query"],
        session_id=search_session_id,
        operation_type=arguments.get("operation_type"),
        limit=_limit(arguments.get("limit"), 20),
    )
    max_chars = _max_chars(arguments.get("max_chars"), 12000)
    output = json.dumps(ops, indent=2, default=str)
    return [TextContent(type="text", text=_truncate_output(output, max_chars, _HINT_GIT))]


async def _tool_list_projects(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    rows = store.sqlite.list_projects(
        keyword=arguments.get("keyword"),
        category=arguments.get("category"),
        limit=_limit(arguments.get("limit"), 20),
    )
    if _bool(arguments.get("verbose"), False):
        output = json.dumps(rows, indent=2, default=str)
    else:
        output = json.dumps(
            [_format_project_compact(r) for r in rows], indent=2, default=str
        )
    return [TextContent(type="text", text=_truncate_output(output, 16000, _HINT_PROJECTS))]


async def _tool_get_project_timeline(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    rows = store.sqlite.list_sessions(
        project_id=arguments["project_id"],
        since=arguments.get("since"),
        until=arguments.get("until"),
        limit=_limit(arguments.get("limit"), 50),
    )
    # Enrich with outcome
    enriched = []
    for r in rows:
        outcome = store.sqlite.get_outcome(r["session_id"])
        enriched.append({
            **r,
            "outcome": outcome["outcome"] if outcome else None,
            "outcome_summary": outcome["summary"] if outcome else None,
        })
    return [TextContent(type="text", text=json.dumps(enriched, indent=2, default=str))]


async def _tool_reconcile(
    store: LonghandStore, arguments: dict[str, Any]
) -> list[TextContent]:
    """Bridge disk↔DB drift from inside an MCP session.

    With `fix=True` (the default for MCP callers): re-ingests any on-disk
    transcripts that are missing from the sessions table or have NULL
    project_id. Closes the loop so Claude can self-heal the index after a
    staleness banner fires, without shelling out to the CLI.
    """
    fix = _bool(arguments.get("fix"), True)
    report = run_reconcile(store, fix=fix)
    output = json.dumps(report.to_dict(), indent=2, default=str)
    return [TextContent(type="text", text=output)]


_DISPATCH: dict[str, Any] = {
    "search": _tool_search,
    "search_in_context": _tool_search_in_context,
    "list_sessions": _tool_list_sessions,
    "get_session_timeline": _tool_get_session_timeline,
    "get_latest_events": _tool_get_latest_events,
    "replay_file": _tool_replay_file,
    "get_file_history": _tool_get_file_history,
    "get_stats": _tool_get_stats,
    "recall": _tool_recall,
    "recall_project_status": _tool_recall_project_status,
    "reconcile": _tool_reconcile,
    "match_project": _tool_match_project,
    "find_episodes": _tool_find_episodes,
    "get_episode": _tool_get_episode,
    "get_session_commits": _tool_get_session_commits,
    "find_commits": _tool_find_commits,
    "list_projects": _tool_list_projects,
    "get_project_timeline": _tool_get_project_timeline,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    handler = _DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"Unknown tool: {name}")]
    store = LonghandStore()
    return await handler(store, arguments)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
