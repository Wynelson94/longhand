"""
Longhand CLI.

Commands:
    longhand ingest [path]       — ingest Claude Code session files
    longhand sessions            — list ingested sessions
    longhand search <query>      — semantic search across events
    longhand timeline <id>       — chronological view of a session
    longhand replay <id> <file>  — reconstruct a file at a point in a session
    longhand diff <event_id>     — show the before/after of a single edit
    longhand stats               — overall storage stats
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

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
    table.add_row("Data directory", str(store.data_dir))

    console.print(table)


if __name__ == "__main__":
    app()
