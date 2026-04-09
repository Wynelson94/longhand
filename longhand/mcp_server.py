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


def _format_event(row: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw SQLite event row into a compact dict for Claude."""
    return {
        "event_id": row["event_id"],
        "session_id": row["session_id"],
        "event_type": row["event_type"],
        "timestamp": row["timestamp"],
        "tool_name": row.get("tool_name"),
        "file_path": row.get("file_path"),
        "content": (row.get("content") or "")[:1500],
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="search",
            description=(
                "Semantic search across all stored Claude Code session events. "
                "Returns events matching a natural language query, with optional "
                "filters by event type, session, tool, or file path."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language query"},
                    "limit": {"type": "integer", "default": 10},
                    "event_type": {
                        "type": "string",
                        "description": "Filter: user_message, assistant_text, assistant_thinking, tool_call, tool_result",
                    },
                    "tool_name": {"type": "string", "description": "Filter by tool name (Edit, Bash, Read, etc.)"},
                    "file_path_contains": {"type": "string", "description": "Filter results where file path contains this string"},
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
            description="Get a chronological timeline of events in a session. Supports session id prefix match.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 200},
                    "include_thinking": {"type": "boolean", "default": True},
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
        hits = store.vectors.search(
            query=arguments["query"],
            n_results=arguments.get("limit", 10),
            event_type=arguments.get("event_type"),
            tool_name=arguments.get("tool_name"),
            file_path_contains=arguments.get("file_path_contains"),
        )
        return [TextContent(type="text", text=json.dumps(hits, indent=2, default=str))]

    if name == "list_sessions":
        rows = store.sqlite.list_sessions(
            project_path=arguments.get("project"),
            limit=arguments.get("limit", 20),
        )
        return [TextContent(type="text", text=json.dumps(rows, indent=2, default=str))]

    if name == "get_session_timeline":
        full_id = _resolve_session_prefix(store, arguments["session_id"])
        if not full_id:
            return [TextContent(type="text", text=f"No session matching: {arguments['session_id']}")]

        events = store.sqlite.get_events(
            session_id=full_id,
            limit=arguments.get("limit", 200),
        )

        include_thinking = arguments.get("include_thinking", True)
        if not include_thinking:
            events = [e for e in events if e["event_type"] != "assistant_thinking"]

        formatted = [_format_event(e) for e in events]
        return [TextContent(type="text", text=json.dumps(formatted, indent=2, default=str))]

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
        return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]

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
