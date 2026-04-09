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
