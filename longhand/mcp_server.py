"""
Longhand MCP server — lets Claude query Longhand memory during live sessions.

Implements the Model Context Protocol so Claude Desktop and Claude Code
can search, retrieve, and replay session data as tool calls.

Run with:
    python -m longhand.mcp_server

Install the `mcp` package to use it:
    pip install mcp
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
except ImportError:
    print(
        "The `mcp` package is required for the MCP server. Install with:\n"
        "    pip install 'longhand[mcp]'",
        file=sys.stderr,
    )
    sys.exit(1)

from longhand.recall import recall as recall_pipeline
from longhand.recall.project_match import match_projects
from longhand.replay import ReplayEngine
from longhand.storage import LonghandStore


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


def _truncate_output(text: str, max_chars: int) -> str:
    """Cap output size and append a pagination hint if truncated."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + (
        "\n\n[... truncated at "
        + str(max_chars)
        + " chars. Use offset/limit, tail, or narrower filters to paginate.]"
    )


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description=(
                "Semantic search across all stored Claude Code session events. "
                "Returns events matching a natural language query, with optional "
                "filters by event type, session, project, tool, or file path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query"},
                    "limit": {"type": "integer", "default": 10},
                    "session_id": {"type": "string", "description": "Scope search to a single session (prefix match)"},
                    "project_id": {"type": "string", "description": "Scope search to a project by project_id"},
                    "project_name": {"type": "string", "description": "Scope search to a project by name substring (e.g. 'gonzo')"},
                    "event_type": {
                        "type": "string",
                        "description": "Filter: user_message, assistant_text, assistant_thinking, tool_call, tool_result",
                    },
                    "tool_name": {"type": "string", "description": "Filter by tool name (Edit, Bash, Read, etc.)"},
                    "file_path_contains": {"type": "string", "description": "Filter results where file path contains this string"},
                    "max_chars": {"type": "integer", "default": 12000, "description": "Max total output characters (default 12000). Set higher if you need full content."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_sessions",
            description="List recent Claude Code sessions that Longhand has indexed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Filter by project path substring"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        ),
        Tool(
            name="get_session_timeline",
            description=(
                "Get a chronological timeline of events in a session. Supports session "
                "id prefix match. Use 'tail' to get only the last N events (great for "
                "checking how a session ended). Use 'offset' to paginate through long sessions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 100, "description": "Max events to return (default 100)"},
                    "offset": {"type": "integer", "default": 0, "description": "Skip first N events (for pagination)"},
                    "tail": {"type": "integer", "description": "Return only the last N events of the session"},
                    "include_thinking": {"type": "boolean", "default": True},
                    "event_type": {"type": "string", "description": "Filter to a single event type"},
                    "summary_only": {
                        "type": "boolean",
                        "default": False,
                        "description": "Return only event_type, timestamp, tool_name, file_path — no content. Great for scanning long sessions.",
                    },
                    "max_chars": {"type": "integer", "default": 16000, "description": "Max total output characters"},
                },
                "required": ["session_id"],
            },
        ),
        Tool(
            name="replay_file",
            description=(
                "Reconstruct the state of a file at a point in a past Claude Code session. "
                "Applies every edit verbatim from the session JSONL — no summarization."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "file_path": {"type": "string"},
                    "at_event_id": {"type": "string", "description": "Optional: reconstruct up to this event"},
                },
                "required": ["session_id", "file_path"],
            },
        ),
        Tool(
            name="get_file_history",
            description="Get every edit ever made to a file across all sessions, chronologically.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "session_id": {"type": "string", "description": "Optional: limit to a single session"},
                },
                "required": ["file_path"],
            },
        ),
        Tool(
            name="get_stats",
            description="Get overall Longhand storage statistics.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="recall",
            description=(
                "PROACTIVE MEMORY. Answer a fuzzy natural-language question about past "
                "Claude Code work. Handles phrases like 'a couple months ago I was building "
                "a game and you fixed a bug'. Returns: matched projects, relevant episodes "
                "(problem→fix pairs), the fix diff, verbatim thinking blocks, reconstructed "
                "file state after the fix, and a prebuilt markdown narrative. Use this as "
                "the FIRST tool for any 'do you remember when...' style question."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language question"},
                    "max_episodes": {"type": "integer", "default": 5},
                    "max_chars": {"type": "integer", "default": 16000, "description": "Max total output characters"},
                },
                "required": ["query"],
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
                    "top_k": {"type": "integer", "default": 5},
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
                    "limit": {"type": "integer", "default": 20},
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
            name="list_projects",
            description="Browse inferred projects by keyword, category, or recency.",
            inputSchema={
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "category": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        ),
        Tool(
            name="get_project_timeline",
            description=(
                "Session-level timeline for a project. Returns recent sessions with their "
                "outcomes (shipped / fixed / stuck / exploratory) for a bird's-eye view of "
                "what's been happening in a project lately."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "since": {"type": "string"},
                    "until": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["project_id"],
            },
        ),
    ]


def _resolve_session_prefix(store: LonghandStore, prefix: str) -> str | None:
    rows = store.sqlite.list_sessions(limit=1000)
    for row in rows:
        if row["session_id"].startswith(prefix):
            return row["session_id"]
    return None


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    store = LonghandStore()

    if name == "search":
        # Resolve session_id prefix if provided
        search_session_id = None
        if arguments.get("session_id"):
            search_session_id = _resolve_session_prefix(store, arguments["session_id"])

        # Resolve project_name → set of session_ids for post-filtering
        project_session_ids: set[str] | None = None
        project_id = arguments.get("project_id")
        project_name = arguments.get("project_name")
        if project_id or project_name:
            # Find sessions belonging to this project
            if project_name and not project_id:
                projects = store.sqlite.list_projects(keyword=project_name, limit=5)
                if projects:
                    project_id = projects[0]["project_id"]
            if project_id:
                proj_sessions = store.sqlite.list_sessions(project_id=project_id, limit=1000)
                project_session_ids = {s["session_id"] for s in proj_sessions}

        hits = store.vectors.search(
            query=arguments["query"],
            n_results=arguments.get("limit", 10) * (3 if project_session_ids else 1),
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
            hits = hits[: arguments.get("limit", 10)]

        max_chars = arguments.get("max_chars", 12000)
        output = json.dumps(hits, indent=2, default=str)
        return [TextContent(type="text", text=_truncate_output(output, max_chars))]

    if name == "list_sessions":
        rows = store.sqlite.list_sessions(
            project_path=arguments.get("project"),
            limit=arguments.get("limit", 20),
        )
        output = json.dumps(rows, indent=2, default=str)
        return [TextContent(type="text", text=_truncate_output(output, 16000))]

    if name == "get_session_timeline":
        full_id = _resolve_session_prefix(store, arguments["session_id"])
        if not full_id:
            return [TextContent(type="text", text=f"No session matching: {arguments['session_id']}")]

        tail = arguments.get("tail")
        offset = arguments.get("offset", 0)
        limit = arguments.get("limit", 100)

        if tail:
            # For tail: fetch all events (up to a reasonable cap) then slice the end
            all_events = store.sqlite.get_events(
                session_id=full_id,
                event_type=arguments.get("event_type"),
                limit=5000,
            )
            include_thinking = arguments.get("include_thinking", True)
            if not include_thinking:
                all_events = [e for e in all_events if e["event_type"] != "assistant_thinking"]
            events = all_events[-tail:]
        else:
            events = store.sqlite.get_events(
                session_id=full_id,
                event_type=arguments.get("event_type"),
                limit=limit,
                offset=offset,
            )
            include_thinking = arguments.get("include_thinking", True)
            if not include_thinking:
                events = [e for e in events if e["event_type"] != "assistant_thinking"]

        summary_only = arguments.get("summary_only", False)
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
        meta = {"session_id": full_id, "returned": len(formatted), "offset": offset}
        if tail:
            meta["tail"] = tail
        payload = {"meta": meta, "events": formatted}

        max_chars = arguments.get("max_chars", 16000)
        output = json.dumps(payload, indent=2, default=str)
        return [TextContent(type="text", text=_truncate_output(output, max_chars))]

    if name == "replay_file":
        full_id = _resolve_session_prefix(store, arguments["session_id"])
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

    if name == "get_file_history":
        engine = ReplayEngine(store.sqlite)
        full_session = None
        if arguments.get("session_id"):
            full_session = _resolve_session_prefix(store, arguments["session_id"])
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

    if name == "get_stats":
        stats = store.stats()
        return [TextContent(type="text", text=json.dumps(stats, indent=2, default=str))]

    # ─── Proactive memory tools (v0.2) ─────────────────────────────────────

    if name == "recall":
        result = recall_pipeline(
            store=store,
            query=arguments["query"],
            max_episodes=arguments.get("max_episodes", 5),
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
            "artifacts": result.artifacts,
            "narrative": result.narrative,
        }
        max_chars = arguments.get("max_chars", 16000)
        output = json.dumps(payload, indent=2, default=str)
        return [TextContent(type="text", text=_truncate_output(output, max_chars))]

    if name == "match_project":
        matches = match_projects(
            store=store,
            query=arguments["query"],
            top_k=arguments.get("top_k", 5),
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

    if name == "find_episodes":
        episodes = store.sqlite.query_episodes(
            project_ids=arguments.get("project_ids"),
            since=arguments.get("since"),
            until=arguments.get("until"),
            keyword=arguments.get("keyword"),
            limit=arguments.get("limit", 20),
        )
        if arguments.get("has_fix", True):
            episodes = [e for e in episodes if e.get("fix_event_id")]
        return [TextContent(type="text", text=json.dumps(episodes, indent=2, default=str))]

    if name == "get_episode":
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

    if name == "list_projects":
        rows = store.sqlite.list_projects(
            keyword=arguments.get("keyword"),
            category=arguments.get("category"),
            limit=arguments.get("limit", 50),
        )
        return [TextContent(type="text", text=json.dumps(rows, indent=2, default=str))]

    if name == "get_project_timeline":
        rows = store.sqlite.list_sessions(
            project_id=arguments["project_id"],
            since=arguments.get("since"),
            until=arguments.get("until"),
            limit=arguments.get("limit", 50),
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

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


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
