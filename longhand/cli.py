"""
Longhand CLI.

Commands:
    longhand ingest [path]          — ingest Claude Code session files
    longhand sessions               — list ingested sessions
    longhand search <query>         — semantic search across events
    longhand timeline <id>          — chronological view of a session
    longhand replay <id> <file>     — reconstruct a file at a point in a session
    longhand diff <event_id>        — show the before/after of a single edit
    longhand stats                  — overall storage stats
    longhand recall "<question>"    — proactive memory: fuzzy natural-language recall
    longhand analyze                — re-run analysis on existing sessions
    longhand projects               — list inferred projects
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from longhand.recall import recall as recall_pipeline
from longhand.setup_commands import (
    doctor as _doctor,
    hook_install as _hook_install,
    hook_uninstall as _hook_uninstall,
    ingest_single_session as _ingest_single,
    mcp_install as _mcp_install,
    mcp_uninstall as _mcp_uninstall,
)

from longhand.parser import JSONLParser, discover_sessions
from longhand.replay import ReplayEngine
from longhand.storage import LonghandStore


app = typer.Typer(
    name="longhand",
    help="Lossless local memory for Claude Code sessions. The full, unabbreviated version.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _get_store(data_dir: Optional[str] = None) -> LonghandStore:
    return LonghandStore(data_dir=data_dir)


def _format_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]


# -----------------------------------------------------------------------------
# INGEST
# -----------------------------------------------------------------------------

@app.command()
def ingest(
    path: Optional[str] = typer.Argument(
        None,
        help="Path to a .jsonl file or directory. Defaults to ~/.claude/projects",
    ),
    data_dir: Optional[str] = typer.Option(None, "--data-dir", help="Longhand data directory"),
    force: bool = typer.Option(False, "--force", help="Re-ingest already-indexed files"),
    limit: int = typer.Option(0, "--limit", help="Max number of sessions to ingest (0 = all)"),
):
    """Ingest Claude Code session JSONL files into the Longhand store."""
    store = _get_store(data_dir)

    if path:
        target = Path(path).expanduser()
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = discover_sessions(target)
        else:
            console.print(f"[red]Path not found: {path}[/red]")
            raise typer.Exit(1)
    else:
        files = discover_sessions()

    if not files:
        console.print("[yellow]No session files found.[/yellow]")
        console.print("Default location: ~/.claude/projects")
        return

    if limit > 0:
        files = files[:limit]

    console.print(f"[cyan]Found {len(files)} session file(s)[/cyan]")

    ingested = 0
    skipped = 0
    events_total = 0
    errors = 0

    for file in files:
        try:
            file_size = file.stat().st_size
            if not force and store.sqlite.already_ingested(str(file), file_size):
                skipped += 1
                continue

            parser = JSONLParser(file)
            events = list(parser.parse_events())
            if not events:
                skipped += 1
                continue

            session = parser.build_session(events)
            result = store.ingest_session(session, events)
            events_total += result["events_stored"]
            ingested += 1

            console.print(
                f"  [green]✓[/green] {session.session_id[:8]} "
                f"[dim]{len(events)} events[/dim] "
                f"[dim]{session.project_path or '—'}[/dim]"
            )
        except Exception as e:
            errors += 1
            console.print(f"  [red]✗[/red] {file.name}: {e}")

    console.print()
    console.print(
        f"[bold]Ingested {ingested}[/bold] sessions, "
        f"[bold]{events_total}[/bold] events "
        f"([dim]{skipped} skipped, {errors} errors[/dim])"
    )


# -----------------------------------------------------------------------------
# SESSIONS
# -----------------------------------------------------------------------------

@app.command()
def sessions(
    project: Optional[str] = typer.Option(None, "--project", help="Filter by project path substring"),
    limit: int = typer.Option(20, "--limit", help="Max sessions to show"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """List ingested sessions."""
    store = _get_store(data_dir)
    rows = store.sqlite.list_sessions(project_path=project, limit=limit)

    if not rows:
        console.print("[yellow]No sessions found. Run `longhand ingest` first.[/yellow]")
        return

    table = Table(title=f"Longhand Sessions ({len(rows)})", show_lines=False)
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Started", style="dim")
    table.add_column("Events", justify="right")
    table.add_column("Edits", justify="right")
    table.add_column("Model", style="magenta")
    table.add_column("Project", style="green", overflow="fold")

    for row in rows:
        table.add_row(
            row["session_id"][:8],
            _format_timestamp(row["started_at"]),
            str(row["event_count"]),
            str(row["file_edit_count"]),
            (row.get("model") or "—")[:16],
            (row.get("project_path") or "—")[-50:],
        )

    console.print(table)


# -----------------------------------------------------------------------------
# SEARCH
# -----------------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(..., help="Semantic query text"),
    limit: int = typer.Option(10, "--limit", "-n"),
    event_type: Optional[str] = typer.Option(
        None, "--type", help="Filter: user_message, assistant_text, assistant_thinking, tool_call, tool_result"
    ),
    session: Optional[str] = typer.Option(None, "--session", help="Filter by session id (prefix match)"),
    tool: Optional[str] = typer.Option(None, "--tool", help="Filter by tool name (Edit, Bash, Read, etc.)"),
    file: Optional[str] = typer.Option(None, "--file", help="Filter by file path substring"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Semantic search across all stored events."""
    store = _get_store(data_dir)

    # Resolve session prefix to full id if provided
    resolved_session = None
    if session:
        rows = store.sqlite.list_sessions(limit=1000)
        for r in rows:
            if r["session_id"].startswith(session):
                resolved_session = r["session_id"]
                break

    hits = store.vectors.search(
        query=query,
        n_results=limit,
        event_type=event_type,
        session_id=resolved_session,
        tool_name=tool,
        file_path_contains=file,
    )

    if not hits:
        console.print("[yellow]No results.[/yellow]")
        return

    for i, hit in enumerate(hits, start=1):
        meta = hit["metadata"]
        distance = hit["distance"]
        relevance = max(0.0, 1.0 - distance)

        header = Text()
        header.append(f"[{i}] ", style="bold cyan")
        header.append(f"{meta.get('event_type', '?')} ", style="yellow")
        if meta.get("tool_name"):
            header.append(f"({meta['tool_name']}) ", style="magenta")
        header.append(f"{_format_timestamp(meta.get('timestamp', ''))} ", style="dim")
        header.append(f"[{relevance:.2f}]", style="green" if relevance > 0.5 else "dim")

        snippet = hit["document"]
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."

        console.print(header)
        console.print(Panel(snippet, border_style="dim", padding=(0, 1)))
        console.print(f"  [dim]event_id:[/dim] {hit['event_id']}")
        if meta.get("file_path"):
            console.print(f"  [dim]file:[/dim] {meta['file_path']}")
        console.print()


# -----------------------------------------------------------------------------
# TIMELINE
# -----------------------------------------------------------------------------

@app.command()
def timeline(
    session_id: str = typer.Argument(..., help="Session ID (prefix match supported)"),
    limit: int = typer.Option(100, "--limit", "-n"),
    show_thinking: bool = typer.Option(True, "--thinking/--no-thinking"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show a chronological timeline of a session."""
    store = _get_store(data_dir)

    # Resolve prefix
    sessions_rows = store.sqlite.list_sessions(limit=1000)
    full_id = None
    for row in sessions_rows:
        if row["session_id"].startswith(session_id):
            full_id = row["session_id"]
            break

    if not full_id:
        console.print(f"[red]No session found matching: {session_id}[/red]")
        raise typer.Exit(1)

    events = store.sqlite.get_events(session_id=full_id, limit=limit)

    if not events:
        console.print("[yellow]No events in session.[/yellow]")
        return

    session_row = store.sqlite.get_session(full_id)
    if session_row:
        console.print(
            Panel.fit(
                f"[bold]{full_id}[/bold]\n"
                f"[dim]{session_row.get('project_path') or '—'}[/dim]\n"
                f"Started: {_format_timestamp(session_row['started_at'])}  "
                f"Events: {session_row['event_count']}",
                title="Session",
                border_style="cyan",
            )
        )
        console.print()

    for e in events:
        etype = e["event_type"]
        if not show_thinking and etype == "assistant_thinking":
            continue

        ts = _format_timestamp(e["timestamp"])
        marker = _event_marker(etype)
        content = (e.get("content") or "").strip()
        if len(content) > 200:
            content = content[:200] + "..."
        content = content.replace("\n", " ")

        line = Text()
        line.append(f"{ts}  ", style="dim")
        line.append(f"{marker} ", style="bold")
        if e.get("tool_name"):
            line.append(f"{e['tool_name']} ", style="magenta")
        if e.get("file_path"):
            line.append(f"{Path(e['file_path']).name} ", style="green")
        line.append(content, style="white")

        console.print(line)


def _event_marker(event_type: str) -> str:
    return {
        "user_message": "👤",
        "assistant_text": "💬",
        "assistant_thinking": "🧠",
        "tool_call": "🔧",
        "tool_result": "✓",
        "file_snapshot": "📸",
        "system": "⚙",
    }.get(event_type, "•")


# -----------------------------------------------------------------------------
# REPLAY
# -----------------------------------------------------------------------------

@app.command()
def replay(
    session_id: str = typer.Argument(..., help="Session ID (prefix match)"),
    file_path: str = typer.Argument(..., help="File path to reconstruct"),
    at_event: Optional[str] = typer.Option(None, "--at-event", help="Reconstruct state at this event id"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Reconstruct the state of a file at a point in a session."""
    store = _get_store(data_dir)

    sessions_rows = store.sqlite.list_sessions(limit=1000)
    full_id = None
    for row in sessions_rows:
        if row["session_id"].startswith(session_id):
            full_id = row["session_id"]
            break
    if not full_id:
        console.print(f"[red]No session matching: {session_id}[/red]")
        raise typer.Exit(1)

    engine = ReplayEngine(store.sqlite)
    state = engine.file_state_at(
        file_path=file_path,
        session_id=full_id,
        at_event_id=at_event,
    )

    if not state:
        console.print(f"[yellow]No edits found for {file_path} in session {full_id[:8]}[/yellow]")
        return

    console.print(
        Panel.fit(
            f"[bold]{state.file_path}[/bold]\n"
            f"[dim]Session:[/dim] {state.session_id[:8]}\n"
            f"[dim]At:[/dim] {state.at_timestamp.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"[dim]Source:[/dim] {state.source}\n"
            f"[dim]Edits applied:[/dim] {state.edits_applied}",
            title="Reconstructed State",
            border_style="cyan",
        )
    )

    # Syntax-highlight if we can guess the language
    language = _guess_language(file_path)
    try:
        console.print(Syntax(state.content, language, theme="monokai", line_numbers=True))
    except Exception:
        console.print(state.content)


def _guess_language(file_path: str) -> str:
    ext = Path(file_path).suffix.lower()
    return {
        ".py": "python",
        ".ts": "typescript",
        ".tsx": "tsx",
        ".js": "javascript",
        ".jsx": "jsx",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".java": "java",
        ".sql": "sql",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".html": "html",
        ".css": "css",
        ".sh": "bash",
        ".toml": "toml",
    }.get(ext, "text")


# -----------------------------------------------------------------------------
# DIFF
# -----------------------------------------------------------------------------

@app.command()
def diff(
    event_id: str = typer.Argument(..., help="Event ID of an edit"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show the before/after content of a single edit event."""
    store = _get_store(data_dir)
    engine = ReplayEngine(store.sqlite)
    result = engine.diff_edit(event_id)

    if not result:
        console.print(f"[red]No edit event found: {event_id}[/red]")
        raise typer.Exit(1)

    console.print(Panel.fit(
        f"[bold]{result['tool_name']}[/bold]  [dim]{result['file_path']}[/dim]",
        border_style="cyan",
    ))

    console.print("[red]--- old[/red]")
    console.print(Panel(result["old"] or "[dim](empty)[/dim]", border_style="red", padding=(0, 1)))
    console.print("[green]+++ new[/green]")
    console.print(Panel(result["new"] or "[dim](empty)[/dim]", border_style="green", padding=(0, 1)))


# -----------------------------------------------------------------------------
# STATS
# -----------------------------------------------------------------------------

@app.command()
def stats(
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show overall storage statistics."""
    store = _get_store(data_dir)
    s = store.stats()

    table = Table(title="Longhand Storage", show_header=False, border_style="cyan")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right", style="cyan")

    table.add_row("Sessions", f"{s['sessions']:,}")
    table.add_row("Events", f"{s['events']:,}")
    table.add_row("Tool calls", f"{s['tool_calls']:,}")
    table.add_row("Thinking blocks", f"{s['thinking_blocks']:,}")
    table.add_row("File edits", f"{s['file_edits']:,}")
    table.add_row("Vectors indexed", f"{s['vectors_indexed']:,}")
    if "projects" in s:
        table.add_row("Projects", f"{s['projects']:,}")
    if "episodes" in s:
        table.add_row("Episodes", f"{s['episodes']:,}")
        resolved = s.get("resolved_episodes", 0)
        table.add_row("Resolved episodes", f"{resolved:,}")
    table.add_row("Data directory", str(store.data_dir))

    console.print(table)


# -----------------------------------------------------------------------------
# RECALL (proactive memory)
# -----------------------------------------------------------------------------

@app.command()
def recall(
    query: str = typer.Argument(..., help="Fuzzy natural-language question about past work"),
    max_episodes: int = typer.Option(5, "--max", "-n"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
    show_raw: bool = typer.Option(False, "--raw", help="Also print raw episode data"),
):
    """Proactive memory: answer a fuzzy question about past Claude Code work."""
    store = _get_store(data_dir)
    result = recall_pipeline(store, query, max_episodes=max_episodes)

    # Show project matches
    if result.project_matches:
        pm_lines = []
        for pm in result.project_matches[:3]:
            reasons = ", ".join(pm.reasons[:3])
            pm_lines.append(f"[cyan]{pm.display_name}[/cyan] [dim]({pm.category or '—'}) · {reasons} · {pm.score:.2f}[/dim]")
        console.print(Panel("\n".join(pm_lines), title="Project matches", border_style="cyan"))

    # Show time window
    since, until = result.time_window
    if since or until:
        since_str = since.strftime("%Y-%m-%d") if since else "—"
        until_str = until.strftime("%Y-%m-%d") if until else "now"
        console.print(f"[dim]Time window:[/dim] {since_str} → {until_str}\n")

    # Narrative
    console.print(Markdown(result.narrative))

    # Raw
    if show_raw and result.episodes:
        console.print()
        console.print(Panel.fit(f"[bold]{len(result.episodes)} episode(s)[/bold]", border_style="dim"))
        for ep in result.episodes:
            console.print(f"  [cyan]{ep['episode_id']}[/cyan]  [dim]{ep.get('ended_at', '')[:16]}[/dim]  confidence={ep.get('confidence', 0):.2f}  status={ep.get('status', '?')}")


# -----------------------------------------------------------------------------
# ANALYZE (backfill analysis on already-ingested sessions)
# -----------------------------------------------------------------------------

@app.command()
def analyze(
    all_sessions: bool = typer.Option(False, "--all", help="Re-analyze every ingested session"),
    session: Optional[str] = typer.Option(None, "--session", help="Re-analyze a single session (prefix match)"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Re-run analysis (projects, outcomes, episodes) on already-ingested sessions."""
    store = _get_store(data_dir)

    if not all_sessions and not session:
        console.print("[yellow]Specify --all or --session <id>[/yellow]")
        raise typer.Exit(1)

    sessions = store.sqlite.list_sessions(limit=10000)
    if session:
        sessions = [s for s in sessions if s["session_id"].startswith(session)]

    if not sessions:
        console.print("[yellow]No matching sessions.[/yellow]")
        return

    console.print(f"[cyan]Analyzing {len(sessions)} session(s)...[/cyan]")
    analyzed = 0
    errors = 0
    total_episodes = 0

    for sess_row in sessions:
        try:
            # Re-parse the transcript file for full event objects
            transcript_path = sess_row["transcript_path"]
            if not Path(transcript_path).exists():
                errors += 1
                continue
            parser = JSONLParser(transcript_path)
            events = list(parser.parse_events())
            if not events:
                continue
            session_obj = parser.build_session(events)
            result = store.analyze_session(session_obj, events)
            total_episodes += result.get("episodes", 0)
            analyzed += 1
            if analyzed % 20 == 0:
                console.print(f"  [dim]{analyzed}/{len(sessions)}...[/dim]")
        except Exception as e:
            errors += 1
            console.print(f"  [red]✗[/red] {sess_row['session_id'][:8]}: {e}")

    console.print()
    console.print(
        f"[bold]Analyzed {analyzed}[/bold] sessions, "
        f"[bold]{total_episodes}[/bold] episodes "
        f"([dim]{errors} errors[/dim])"
    )


# -----------------------------------------------------------------------------
# PROJECTS
# -----------------------------------------------------------------------------

@app.command()
def projects(
    keyword: Optional[str] = typer.Option(None, "--keyword", "-k"),
    category: Optional[str] = typer.Option(None, "--category", "-c"),
    limit: int = typer.Option(50, "--limit"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """List inferred projects."""
    store = _get_store(data_dir)
    rows = store.sqlite.list_projects(keyword=keyword, category=category, limit=limit)

    if not rows:
        console.print("[yellow]No projects found. Run `longhand analyze --all` first.[/yellow]")
        return

    table = Table(title=f"Longhand Projects ({len(rows)})", show_lines=False)
    table.add_column("Project", style="cyan", no_wrap=True)
    table.add_column("Category", style="magenta")
    table.add_column("Sessions", justify="right")
    table.add_column("Edits", justify="right")
    table.add_column("Last seen", style="dim")
    table.add_column("Path", style="green", overflow="fold")

    for row in rows:
        table.add_row(
            row["display_name"][:30],
            row.get("category") or "—",
            str(row["session_count"]),
            str(row["total_edits"]),
            _format_timestamp(row["last_seen"]),
            row["canonical_path"][-50:],
        )

    console.print(table)


# -----------------------------------------------------------------------------
# HISTORY — cross-session file history
# -----------------------------------------------------------------------------

@app.command()
def history(
    file_path: str = typer.Argument(..., help="File path (exact or substring match)"),
    limit: int = typer.Option(50, "--limit"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show every edit to a file across all sessions, chronologically."""
    store = _get_store(data_dir)

    # Try exact match first
    edits = store.sqlite.get_file_edits(file_path)

    # Fall back to substring match
    if not edits:
        events = store.sqlite.get_events(
            file_path=file_path, event_type="tool_call", limit=limit * 4
        )
        edits = [
            e for e in events
            if e.get("file_operation") in ("edit", "write", "multi_edit", "notebook_edit")
        ]

    if not edits:
        console.print(f"[yellow]No edits found for '{file_path}'[/yellow]")
        return

    edits = edits[:limit]

    # Group by resolved file path to handle substring matches touching multiple files
    unique_files = sorted({e.get("file_path") or file_path for e in edits})
    unique_sessions = len({e["session_id"] for e in edits})

    console.print(
        Panel.fit(
            f"[bold]{file_path}[/bold]\n"
            f"[dim]{len(edits)} edit(s) across "
            f"{unique_sessions} session(s) · {len(unique_files)} matching file(s)[/dim]",
            title="File History",
            border_style="cyan",
        )
    )

    last_session: str | None = None
    last_file: str | None = None
    for e in edits:
        session_id = e["session_id"]
        resolved_file = e.get("file_path") or ""

        if session_id != last_session:
            console.print()
            console.print(f"[cyan]Session {session_id[:8]}[/cyan]")
            last_session = session_id
            last_file = None

        if resolved_file != last_file and len(unique_files) > 1:
            console.print(f"  [green]{resolved_file}[/green]")
            last_file = resolved_file

        ts = _format_timestamp(e["timestamp"])
        tool = e.get("tool_name") or "?"

        # Short preview of the change
        old = (e.get("old_content") or "").strip().replace("\n", " ")[:60]
        new = (e.get("new_content") or "").strip().replace("\n", " ")[:60]
        preview = ""
        if tool == "Edit" and old and new:
            preview = f"[red]{old}[/red] → [green]{new}[/green]"
        elif tool == "Write":
            char_count = len(e.get("new_content") or "")
            preview = f"[green]wrote {char_count} chars[/green]"
        elif tool == "MultiEdit":
            preview = "[green]multi-edit[/green]"

        console.print(f"    [dim]{ts}[/dim] {tool}  {preview}")
        console.print(f"      [dim]event:[/dim] {e['event_id']}")


# -----------------------------------------------------------------------------
# INSTALL / AUTO-INGEST / DOCTOR
# -----------------------------------------------------------------------------

hook_app = typer.Typer(name="hook", help="Claude Code SessionEnd hook (auto-ingest)")
mcp_app = typer.Typer(name="mcp", help="Claude Desktop MCP server integration")

app.add_typer(hook_app, name="hook")
app.add_typer(mcp_app, name="mcp")


@hook_app.command("install")
def hook_install_cmd():
    """Install the SessionEnd hook into ~/.claude/settings.json."""
    _hook_install()


@hook_app.command("uninstall")
def hook_uninstall_cmd():
    """Remove the SessionEnd hook."""
    _hook_uninstall()


@mcp_app.command("install")
def mcp_install_cmd():
    """Install Longhand's MCP server into Claude Desktop."""
    _mcp_install()


@mcp_app.command("uninstall")
def mcp_uninstall_cmd():
    """Remove Longhand's MCP server from Claude Desktop."""
    _mcp_uninstall()


@mcp_app.command("serve")
def mcp_serve_cmd():
    """Run the MCP server (stdio). Used by Claude Desktop."""
    import asyncio
    from longhand.mcp_server import main as mcp_main
    asyncio.run(mcp_main())


# Short alias so `longhand mcp-server` also works (matches install command)
@app.command("mcp-server")
def mcp_server_cmd():
    """Run the MCP server (stdio). Used by Claude Desktop."""
    import asyncio
    from longhand.mcp_server import main as mcp_main
    asyncio.run(mcp_main())


@app.command("ingest-session")
def ingest_session_cmd(
    transcript: str = typer.Option(..., "--transcript", "-t", help="Path to a single session JSONL"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Ingest a single session file (called by the SessionEnd hook)."""
    _ingest_single(transcript, data_dir)


@app.command()
def doctor():
    """Diagnose Longhand installation and data."""
    _doctor()


if __name__ == "__main__":
    app()
