"""Bounded keyword retrieval from the shared archive, with no vector model.

Run as ``python -m longhand.lightweight_mcp``. This deliberately exposes only
read tools. Existing Longhand semantic tools can still use the same archive.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print(
        "The `mcp` package (>= 1.2.0) is required for the shared MCP server. "
        "It ships with Longhand — reinstall with:\n"
        "    pip install --upgrade longhand",
        file=sys.stderr,
    )
    sys.exit(1)

mcp = FastMCP(
    "longhand",
    instructions=(
        "Shared Claude and Codex history. Use search with a short literal phrase, "
        "list_sessions to find sessions, then get_session_timeline for context. "
        "Search is keyword matching, not semantic similarity. Records are exact; "
        "display excerpts are bounded. Use get_event_text to page through longer records."
    ),
)


@contextmanager
def connection():
    root = Path(os.environ.get("LONGHAND_DATA_DIR") or Path.home() / ".longhand")
    conn = sqlite3.connect((root / "longhand.db").resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA cache_size = -2048")
    # Bound scanning work as well as returned data (large archives may need
    # a session filter). SQLite aborts the query after this instruction budget.
    ticks = 0

    def budget():
        nonlocal ticks
        ticks += 1
        return int(ticks > 2000)

    conn.set_progress_handler(budget, 10000)
    try:
        yield conn
    finally:
        conn.close()


def bounded(limit):
    return max(1, min(int(limit), 50))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@mcp.tool()
def list_sessions(
    limit: int = 10,
    offset: int = 0,
    source: str | None = None,
    project: str | None = None,
) -> list[dict]:
    """List recent Claude and Codex sessions from the same local archive.

    `source` narrows to "codex" or "claude"; `project` matches a substring of
    the session's project path.
    """
    where: list[str] = []
    params: list[object] = []
    if source:
        if source not in ("codex", "claude"):
            raise ValueError('source must be "codex" or "claude"')
        where.append(
            "session_id LIKE 'codex:%'" if source == "codex" else "session_id NOT LIKE 'codex:%'"
        )
    if project:
        where.append("project_path LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(project)}%")
    clause = f"WHERE {' AND '.join(where)} " if where else ""
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT session_id, project_path, started_at, ended_at, event_count "
                f"FROM sessions {clause}ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (*params, bounded(limit), max(0, offset)),
            )
        ]


@mcp.tool()
def search(query: str, session_id: str | None = None, limit: int = 10) -> list[dict]:
    """Find a literal phrase in stored text; narrow by session for large archives."""
    if not query.strip() or len(query) > 200:
        raise ValueError("Use a nonempty phrase of at most 200 characters")
    escaped = _escape_like(query)
    where = "content LIKE ? ESCAPE '\\'"
    params = [f"%{escaped}%"]
    if session_id:
        where += " AND session_id = ?"
        params.append(session_id)
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, session_id, sequence, event_type, timestamp, "
                "substr(content, 1, 2000) AS content, length(content) AS total_chars "
                f"FROM events WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                (*params, bounded(limit)),
            )
        ]


@mcp.tool()
def get_session_timeline(session_id: str, offset: int = 0, limit: int = 10) -> list[dict]:
    """Read consecutive recorded events; paginate with offset."""
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, sequence, event_type, timestamp, "
                "substr(content, 1, 2000) AS content, length(content) AS total_chars "
                "FROM events WHERE session_id = ? ORDER BY sequence LIMIT ? OFFSET ?",
                (session_id, bounded(limit), max(0, offset)),
            )
        ]


@mcp.tool()
def get_event_text(event_id: str, offset: int = 0, raw: bool = False) -> dict:
    """Read an exact text slice (up to 8,000 characters), or the stored raw JSON."""
    column = "raw_json" if raw else "content"
    with connection() as conn:
        row = conn.execute(
            f"SELECT substr({column}, ?, 8000) AS text, length({column}) AS total_chars "
            "FROM events WHERE event_id = ?",
            (max(0, offset) + 1, event_id),
        ).fetchone()
        return dict(row) if row else {}


if __name__ == "__main__":
    mcp.run()
