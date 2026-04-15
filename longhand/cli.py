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
    prompt_hook_install as _prompt_hook_install,
    prompt_hook_uninstall as _prompt_hook_uninstall,
    run_prompt_hook as _run_prompt_hook,
)

from longhand.parser import JSONLParser, discover_sessions
from longhand.replay import ReplayEngine
from longhand.storage import LonghandStore


app = typer.Typer(
    name="longhand",
    help="Persistent local memory for Claude Code sessions. Every event, every edit, nothing summarized.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _get_store(data_dir: Optional[str] = None) -> LonghandStore:
    return LonghandStore(data_dir=data_dir)


def _resolve_prefix(store: LonghandStore, prefix: str) -> str | None:
    """Resolve a session ID prefix to a full session ID."""
    rows = store.sqlite.list_sessions(limit=1000)
    for row in rows:
        if row["session_id"].startswith(prefix):
            return row["session_id"]
    return None


def _format_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]


# -----------------------------------------------------------------------------
# SETUP — one-command install wrapper
# -----------------------------------------------------------------------------


@app.command()
def setup(
    skip_ingest: bool = typer.Option(False, "--skip-ingest", help="Skip backfilling existing sessions"),
    skip_prompt_hook: bool = typer.Option(False, "--skip-prompt-hook", help="Skip auto-context injection hook"),
    skip_mcp: bool = typer.Option(False, "--skip-mcp", help="Skip MCP server install"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """One-command setup: ingest existing sessions, install hooks, configure MCP.

    This wraps the five commands you would otherwise run individually:
      - longhand ingest          (backfill existing Claude Code history)
      - longhand analyze --all   (run analysis on every session)
      - longhand hook install    (auto-ingest future sessions)
      - longhand prompt-hook install  (auto-inject past context — optional)
      - longhand mcp install     (expose tools to Claude Code)
      - longhand doctor          (verify everything)

    Takes ~2 minutes on a laptop with a year of sessions. Safe to re-run.
    """
    console.print("[bold cyan]→ Longhand setup[/bold cyan]\n")

    store = _get_store(data_dir)

    # 1. Ingest existing history
    if not skip_ingest:
        console.print("[bold]1/5[/bold] Ingesting existing Claude Code sessions...")
        try:
            target = Path.home() / ".claude" / "projects"
            if target.exists():
                sessions = discover_sessions(target)
                ingested = 0
                for idx, session_path in enumerate(sessions):
                    try:
                        _ingest_single(str(session_path), data_dir)
                        ingested += 1
                    except Exception:
                        pass
                    if idx > 0 and idx % 20 == 0:
                        console.print(f"   {idx}/{len(sessions)}...")
                console.print(f"[green]   ✓[/green] Ingested {ingested} session(s)")
            else:
                console.print(f"[yellow]   ⚠ No Claude Code history found at {target}[/yellow]")
        except Exception as e:
            console.print(f"[red]   ✗ Ingest failed: {e}[/red]")
    else:
        console.print("[dim]1/5[/dim] Skipping ingest")

    # 2. Hook install (auto-ingest future sessions)
    console.print("\n[bold]2/5[/bold] Installing SessionEnd hook (auto-ingest future sessions)...")
    try:
        _hook_install()
        console.print("[green]   ✓[/green] Hook installed")
    except Exception as e:
        console.print(f"[red]   ✗ Hook install failed: {e}[/red]")

    # 3. Prompt hook (optional)
    if not skip_prompt_hook:
        console.print("\n[bold]3/5[/bold] Installing UserPromptSubmit hook (auto-context injection)...")
        try:
            _prompt_hook_install()
            console.print("[green]   ✓[/green] Prompt hook installed")
        except Exception as e:
            console.print(f"[red]   ✗ Prompt hook install failed: {e}[/red]")
    else:
        console.print("\n[dim]3/5[/dim] Skipping prompt hook")

    # 4. MCP install
    if not skip_mcp:
        console.print("\n[bold]4/5[/bold] Installing MCP server for Claude Code...")
        try:
            _mcp_install()
            console.print("[green]   ✓[/green] MCP server registered")
        except Exception as e:
            console.print(f"[red]   ✗ MCP install failed: {e}[/red]")
    else:
        console.print("\n[dim]4/5[/dim] Skipping MCP install")

    # 5. Doctor — verify everything
    console.print("\n[bold]5/5[/bold] Verifying installation...")
    _doctor()

    console.print("\n[bold green]→ Setup complete.[/bold green]")
    console.print("Try: [cyan]longhand recall \"what was I working on\"[/cyan]")
    console.print("Or:  [cyan]longhand status <project-name>[/cyan]")


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

    # Claim the ingest lock so on-the-fly fallback callers can see that
    # a full ingest is in progress (and skip spawning another one). If
    # another live ingest holds the lock, exit cleanly — we're a spawned
    # duplicate from a fallback-path race, not a user invocation.
    from longhand.recall.project_fallback import (
        claim_ingest_lock,
        release_ingest_lock,
    )
    if not claim_ingest_lock(store):
        console.print("[yellow]Another ingest is already running — skipping.[/yellow]")
        return

    if path:
        target = Path(path).expanduser()
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = discover_sessions(target)
        else:
            console.print(f"[red]Path not found: {path}[/red]")
            release_ingest_lock(store)
            raise typer.Exit(1)
    else:
        files = discover_sessions()

    if not files:
        console.print("[yellow]No session files found.[/yellow]")
        console.print("Default location: ~/.claude/projects")
        release_ingest_lock(store)
        return

    if limit > 0:
        files = files[:limit]

    console.print(f"[cyan]Found {len(files)} session file(s)[/cyan]")

    ingested = 0
    skipped = 0
    events_total = 0
    errors = 0

    try:
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
    finally:
        release_ingest_lock(store)


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
# GIT LOG — show git operations from sessions

@app.command()
def git_log(
    session_id: Optional[str] = typer.Argument(None, help="Session ID (prefix match). Shows recent across all sessions if omitted."),
    operation: Optional[str] = typer.Option(None, "--type", "-t", help="Filter by operation type (commit, push, etc.)"),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Search commit messages"),
    limit: int = typer.Option(50, "--limit"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show git operations from Claude Code sessions."""
    store = _get_store(data_dir)

    if query:
        full_session = None
        if session_id:
            full_session = _resolve_prefix(store, session_id)
        ops = store.sqlite.search_git_operations(
            query=query, session_id=full_session, operation_type=operation, limit=limit
        )
    elif session_id:
        full_session = _resolve_prefix(store, session_id)
        if not full_session:
            console.print(f"[red]No session matching: {session_id}[/red]")
            return
        ops = store.sqlite.get_git_operations(
            session_id=full_session, operation_type=operation, limit=limit
        )
    else:
        # Show recent git operations across all sessions
        ops = store.sqlite.search_git_operations(
            query="", operation_type=operation, limit=limit
        )

    if not ops:
        console.print("[yellow]No git operations found.[/yellow]")
        return

    table = Table(title=f"Git Operations ({len(ops)})", show_lines=False)
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Op", style="cyan", no_wrap=True)
    table.add_column("Hash", style="yellow", no_wrap=True)
    table.add_column("Branch", style="magenta")
    table.add_column("Message", overflow="fold")
    table.add_column("Session", style="dim", no_wrap=True)

    for op in ops:
        table.add_row(
            _format_timestamp(op["timestamp"]),
            op["operation_type"],
            (op.get("commit_hash") or "")[:8],
            op.get("branch") or "—",
            (op.get("commit_message") or "")[:60],
            op["session_id"][:8],
        )

    console.print(table)


# -----------------------------------------------------------------------------
# CONFIG — view and edit hook configuration
# -----------------------------------------------------------------------------

@app.command()
def config(
    show: bool = typer.Option(True, "--show/--edit", help="Show current config (default) or open for editing"),
    set_key: Optional[str] = typer.Option(None, "--set", help="Set a config key, e.g. --set hook.min_relevance=3.0"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """View or edit Longhand hook configuration.

    Config lives at ~/.longhand/config.json. Controls prompt hook behavior:
    - hook.min_relevance: minimum score to inject context (default 2.5, higher = less noise)
    - hook.max_inject_chars: max characters injected per prompt (default 2000)
    - hook.max_episodes: max episodes to consider (default 2)
    - hook.enabled: true/false to enable/disable without uninstalling
    """
    import json as _json

    config_path = Path.home() / ".longhand" / "config.json"

    if set_key:
        # Parse key=value
        if "=" not in set_key:
            console.print("[red]Use format: --set hook.key=value[/red]")
            return
        key_path, value_str = set_key.split("=", 1)
        parts = key_path.split(".")
        if len(parts) != 2 or parts[0] != "hook":
            console.print("[red]Only hook.* keys are supported. E.g. --set hook.min_relevance=3.0[/red]")
            return

        # Load existing config
        current: dict = {}
        if config_path.exists():
            try:
                current = _json.loads(config_path.read_text())
            except Exception:
                current = {}

        if "hook" not in current:
            current["hook"] = {}

        # Parse value type
        key = parts[1]
        if value_str.lower() in ("true", "false"):
            current["hook"][key] = value_str.lower() == "true"
        else:
            try:
                current["hook"][key] = float(value_str)
                if current["hook"][key] == int(current["hook"][key]):
                    current["hook"][key] = int(current["hook"][key])
            except ValueError:
                current["hook"][key] = value_str

        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(_json.dumps(current, indent=2) + "\n")
        console.print(f"[green]Set {key_path} = {current['hook'][key]}[/green]")
        return

    # Show current config
    from longhand.setup_commands import _load_hook_config
    hook = _load_hook_config()

    table = Table(title="Longhand Hook Config", show_lines=False)
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="yellow")
    table.add_column("Description", style="dim")

    descriptions = {
        "min_relevance": "Minimum relevance score to inject context (higher = less noise)",
        "max_inject_chars": "Max characters injected per prompt (controls token usage)",
        "max_episodes": "Max episodes to consider per query",
        "enabled": "Whether the prompt hook is active",
    }

    for k, v in hook.items():
        table.add_row(f"hook.{k}", str(v), descriptions.get(k, ""))

    console.print(table)
    console.print(f"\n[dim]Config file: {config_path}[/dim]")
    console.print("[dim]Edit with: longhand config --set hook.min_relevance=3.0[/dim]")


# -----------------------------------------------------------------------------
# CONTEXT — output relevant past context for hook injection
# -----------------------------------------------------------------------------

@app.command()
def context(
    query: str = typer.Argument(..., help="The user's prompt or query"),
    max_episodes: int = typer.Option(2, "--max", "-n"),
    min_relevance: float = typer.Option(
        2.0, "--threshold", help="Minimum rank score to inject context (filters noise)"
    ),
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Restrict to a project name or id"
    ),
    silent_if_empty: bool = typer.Option(
        True, "--silent/--always",
        help="Print nothing when nothing relevant is found (vs. print 'no context')",
    ),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Output relevant past context for a query in a hook-injectable format.

    Designed to be called from a UserPromptSubmit hook so Claude Code can
    automatically pull in past context before responding. Returns plain text
    (no Rich formatting). Returns silently when nothing is relevant enough
    to inject — avoids polluting prompts with low-quality matches.
    """
    import sys
    from longhand.recall import recall as _recall

    store = _get_store(data_dir)

    # Skip very short queries — usually not worth recalling
    if len(query.strip()) < 10:
        if not silent_if_empty:
            print("(query too short for recall)")
        return

    try:
        result = _recall(store, query, max_episodes=max_episodes)
    except Exception as e:
        # Hooks must never crash the parent process
        if not silent_if_empty:
            print(f"(longhand error: {e})", file=sys.stderr)
        return

    # Project filter (post-recall)
    if project and result.episodes:
        proj_id = None
        if project.startswith("p_"):
            proj_id = project
        else:
            matches = store.sqlite.list_projects(keyword=project, limit=5)
            if matches:
                proj_id = matches[0]["project_id"]
        if proj_id:
            result.episodes = [e for e in result.episodes if e.get("project_id") == proj_id]

    # Quality gate — only inject if we have a strong match
    if not result.episodes:
        if not silent_if_empty:
            print("(no relevant past context)")
        return

    top = result.episodes[0]
    confidence = top.get("confidence") or 0.0

    # Compute relevance: high confidence + matching keywords
    import re as _re
    query_words = set(_re.findall(r"[a-z]{4,}", query.lower()))
    searchable = " ".join([
        top.get("problem_description") or "",
        top.get("diagnosis_summary") or "",
        top.get("fix_summary") or "",
    ]).lower()
    keyword_overlap = sum(1 for w in query_words if w in searchable)
    relevance = keyword_overlap * 1.0 + confidence * 2.0

    if relevance < min_relevance:
        if not silent_if_empty:
            print(f"(relevance {relevance:.2f} below threshold {min_relevance})")
        return

    # Build the injection block — plain text, parseable by humans and AIs
    lines: list[str] = []
    lines.append("[Longhand recall — relevant past context]")
    lines.append("")

    # Project info
    if top.get("project_id"):
        proj = store.sqlite.get_project(top["project_id"])
        if proj:
            lines.append(f"Project: {proj['display_name']}")

    # Time
    when = top.get("started_at", "")[:16]
    lines.append(f"Found in session {top['session_id'][:8]} at {when}")
    lines.append("")

    # Problem
    if top.get("problem_description"):
        lines.append("Problem:")
        lines.append(top["problem_description"][:300].strip())
        lines.append("")

    # Diagnosis (if we have one)
    if top.get("diagnosis_summary"):
        lines.append("Diagnosis:")
        lines.append(top["diagnosis_summary"][:400].strip())
        lines.append("")

    # Fix
    if top.get("fix_summary"):
        lines.append("Fix:")
        lines.append(top["fix_summary"][:300])
        lines.append("")

    # Diff (if available)
    fix = result.artifacts.get("fix") if result.artifacts else None
    if fix and (fix.get("old") or fix.get("new")):
        lines.append("Diff:")
        old = (fix.get("old") or "").strip()[:600]
        new = (fix.get("new") or "").strip()[:600]
        if old:
            for line in old.split("\n")[:10]:
                lines.append(f"- {line}")
        if new:
            for line in new.split("\n")[:10]:
                lines.append(f"+ {line}")
        lines.append("")

    # File reference
    if fix and fix.get("file_path"):
        lines.append(f"File: {fix['file_path']}")
        lines.append("")

    lines.append(f"(Use `longhand export {top['episode_id']}` for full detail.)")
    lines.append("[end Longhand recall]")

    # Plain print, not rich.console — hooks consume stdout directly
    print("\n".join(lines))


# -----------------------------------------------------------------------------
# EXPORT — episode or session as standalone markdown
# -----------------------------------------------------------------------------

@app.command()
def export(
    target: str = typer.Argument(
        ...,
        help="Episode ID (ep_*), session ID (prefix), or shortcut: latest, latest-fix",
    ),
    output: Optional[str] = typer.Option(None, "--out", "-o", help="Write to file instead of stdout"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Export an episode or session as standalone markdown."""
    import json as _json

    store = _get_store(data_dir)

    # Resolve shortcut targets
    if target in ("latest", "latest-fix"):
        episodes = store.sqlite.query_episodes(
            status="resolved" if target == "latest-fix" else None,
            limit=1,
        )
        if not episodes:
            console.print("[red]No episodes found.[/red]")
            raise typer.Exit(1)
        target = episodes[0]["episode_id"]

    # Episode export
    if target.startswith("ep_"):
        ep = store.sqlite.get_episode(target)
        if not ep:
            console.print(f"[red]No episode: {target}[/red]")
            raise typer.Exit(1)

        md = _episode_to_markdown(store, ep)

        if output:
            Path(output).write_text(md)
            console.print(f"[green]✓[/green] Exported to {output}")
        else:
            console.print(Markdown(md))
        return

    # Session export — prefix match
    sessions_rows = store.sqlite.list_sessions(limit=1000)
    full_id = None
    for row in sessions_rows:
        if row["session_id"].startswith(target):
            full_id = row["session_id"]
            break

    if not full_id:
        console.print(f"[red]No episode or session matching: {target}[/red]")
        raise typer.Exit(1)

    md = _session_to_markdown(store, full_id)

    if output:
        Path(output).write_text(md)
        console.print(f"[green]✓[/green] Exported to {output}")
    else:
        console.print(Markdown(md))


def _episode_to_markdown(store, ep: dict) -> str:
    """Render an episode as a self-contained markdown document."""
    import json as _json

    lines: list[str] = []

    # Header
    lines.append(f"# Episode: {ep['episode_id']}")
    lines.append("")

    # Project
    if ep.get("project_id"):
        proj = store.sqlite.get_project(ep["project_id"])
        if proj:
            lines.append(f"**Project:** {proj['display_name']} ({proj.get('category') or 'uncategorized'})")
            lines.append(f"**Path:** `{proj['canonical_path']}`")

    lines.append(f"**Session:** `{ep['session_id'][:8]}`")
    lines.append(f"**Started:** {ep.get('started_at', '')}")
    lines.append(f"**Ended:** {ep.get('ended_at', '')}")
    lines.append(f"**Status:** {ep.get('status', 'unknown')}")
    lines.append(f"**Confidence:** {ep.get('confidence', 0):.2f}")
    lines.append("")

    # Problem
    if ep.get("problem_description"):
        lines.append("## What went wrong")
        lines.append("")
        lines.append(ep["problem_description"].strip())
        lines.append("")

    # Diagnosis
    if ep.get("diagnosis_summary"):
        lines.append("## Diagnosis (verbatim thinking block)")
        lines.append("")
        lines.append("```")
        lines.append(ep["diagnosis_summary"].strip())
        lines.append("```")
        lines.append("")

    # Fix
    fix_id = ep.get("fix_event_id")
    if fix_id:
        fix_event = store.sqlite.get_event(fix_id)
        if fix_event:
            lines.append("## The fix")
            lines.append("")
            lines.append(f"**File:** `{fix_event.get('file_path') or '?'}`")
            lines.append(f"**Tool:** `{fix_event.get('tool_name') or '?'}`")
            lines.append("")
            lines.append("```diff")
            old_lines = (fix_event.get("old_content") or "").splitlines() or [""]
            new_lines = (fix_event.get("new_content") or "").splitlines() or [""]
            for line in old_lines:
                lines.append(f"- {line}")
            for line in new_lines:
                lines.append(f"+ {line}")
            lines.append("```")
            lines.append("")

            # Reconstructed file state
            from longhand.replay import ReplayEngine
            engine = ReplayEngine(store.sqlite)
            try:
                state = engine.file_state_at(
                    file_path=fix_event["file_path"],
                    session_id=ep["session_id"],
                    at_event_id=fix_id,
                )
                if state and state.content:
                    lines.append("## File state after the fix")
                    lines.append("")
                    ext = Path(fix_event["file_path"]).suffix.lstrip(".") or "text"
                    lines.append(f"```{ext}")
                    lines.append(state.content)
                    lines.append("```")
                    lines.append("")
            except Exception:
                pass

    # Verification
    if ep.get("verification_event_id"):
        lines.append("## Verification")
        lines.append("")
        lines.append("✓ A test or command succeeded after the fix.")
        lines.append("")

    # Touched files
    if ep.get("touched_files_json"):
        try:
            touched = _json.loads(ep["touched_files_json"])
            if touched:
                lines.append("## Touched files")
                lines.append("")
                for f in touched:
                    lines.append(f"- `{f}`")
                lines.append("")
        except Exception:
            pass

    lines.append("---")
    lines.append("*Exported from Longhand*")

    return "\n".join(lines)


def _session_to_markdown(store, session_id: str) -> str:
    """Render a session timeline as markdown."""
    import json as _json

    lines: list[str] = []
    session = store.sqlite.get_session(session_id)
    if not session:
        return f"# Session not found: {session_id}"

    lines.append(f"# Session: {session_id[:8]}")
    lines.append("")

    if session.get("project_id"):
        proj = store.sqlite.get_project(session["project_id"])
        if proj:
            lines.append(f"**Project:** {proj['display_name']}")
            lines.append(f"**Path:** `{proj['canonical_path']}`")

    lines.append(f"**Started:** {session.get('started_at', '')}")
    lines.append(f"**Ended:** {session.get('ended_at', '')}")
    lines.append(f"**Events:** {session.get('event_count', 0)}")

    outcome = store.sqlite.get_outcome(session_id)
    if outcome:
        lines.append(f"**Outcome:** {outcome['outcome']} ({outcome.get('confidence', 0):.2f})")
    lines.append("")

    # Episodes
    eps = store.sqlite.query_episodes(session_id=session_id, limit=50)
    if eps:
        lines.append(f"## Episodes ({len(eps)})")
        lines.append("")
        for ep in eps:
            status = ep.get("status", "?")
            problem = (ep.get("problem_description") or "")[:100]
            lines.append(f"- **{status}** — {problem}")
        lines.append("")

    # Timeline (visible events only)
    all_events = store.sqlite.get_events(session_id=session_id, limit=10000)
    visible = [e for e in all_events if e["event_type"] in ("user_message", "assistant_text", "tool_call", "tool_result")]

    lines.append(f"## Timeline ({len(visible)} events)")
    lines.append("")

    for e in visible:
        etype = e["event_type"]
        ts = e["timestamp"][:16] if e.get("timestamp") else ""

        if etype == "user_message":
            content = (e.get("content") or "").strip()[:300]
            lines.append(f"**{ts}** USER")
            lines.append("")
            lines.append(f"> {content}")
            lines.append("")
        elif etype == "assistant_text":
            content = (e.get("content") or "").strip()[:300]
            lines.append(f"**{ts}** CLAUDE")
            lines.append("")
            lines.append(content)
            lines.append("")
        elif etype == "tool_call":
            tool = e.get("tool_name") or "?"
            file_path = e.get("file_path") or ""
            extra = f" `{Path(file_path).name}`" if file_path else ""
            lines.append(f"**{ts}** 🔧 {tool}{extra}")
            lines.append("")
        elif etype == "tool_result":
            status_marker = "❌" if e.get("error_detected") else "✓"
            lines.append(f"**{ts}** {status_marker}")
            if e.get("error_detected") and e.get("error_snippet"):
                lines.append(f"> {e['error_snippet'][:200]}")
            lines.append("")

    lines.append("---")
    lines.append("*Exported from Longhand*")
    return "\n".join(lines)


# -----------------------------------------------------------------------------
# PATTERNS — recurring fix patterns across episodes
# -----------------------------------------------------------------------------

@app.command()
def patterns(
    limit: int = typer.Option(10, "--limit", "-n", help="Top N pattern groups to show"),
    min_count: int = typer.Option(2, "--min", help="Minimum episode count per pattern"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show recurring fix patterns across all episodes — bugs you keep fixing.

    Groups episodes by error category and shared keywords from problem descriptions.
    """
    import json as _json
    import re as _re
    from collections import Counter, defaultdict

    store = _get_store(data_dir)

    # Pull all resolved/partial episodes
    episodes = store.sqlite.query_episodes(limit=10000)

    if not episodes:
        console.print("[yellow]No episodes found. Run `longhand analyze --all` first.[/yellow]")
        return

    # Group by (category-tag, normalized error keyword)
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for ep in episodes:
        problem = (ep.get("problem_description") or "")[:300].lower()

        # Pull tags
        tags: list[str] = []
        if ep.get("tags_json"):
            try:
                tags = _json.loads(ep["tags_json"])
            except Exception:
                pass
        category = tags[0] if tags else "unknown"

        # Extract distinctive keywords from problem
        words = _re.findall(r"[a-z][a-z0-9]{4,}", problem)
        # Remove common words
        common = {
            "error", "type", "test", "tests", "module", "import", "imports", "fail",
            "failed", "value", "result", "check", "found", "missing", "expected",
            "received", "actual", "passed", "running",
        }
        meaningful = [w for w in words if w not in common]
        # Most distinctive token (the longest non-common one)
        keyword = sorted(meaningful, key=len, reverse=True)[0] if meaningful else ""

        if not keyword:
            keyword = "general"

        groups[(category, keyword)].append(ep)

    # Sort groups by count
    ranked = sorted(
        [(key, eps) for key, eps in groups.items() if len(eps) >= min_count],
        key=lambda kv: len(kv[1]),
        reverse=True,
    )[:limit]

    if not ranked:
        console.print(f"[yellow]No patterns found with at least {min_count} occurrences.[/yellow]")
        return

    console.print(
        Panel.fit(
            f"[bold]Recurring fix patterns[/bold]\n"
            f"[dim]Found {len(ranked)} pattern(s) across {len(episodes)} episode(s)[/dim]",
            border_style="cyan",
        )
    )
    console.print()

    for (category, keyword), eps in ranked:
        # Header
        header = Text()
        header.append(f"× {len(eps)}  ", style="bold cyan")
        header.append(f"{category}", style="magenta")
        if keyword and keyword != "general":
            header.append(f" · {keyword}", style="yellow")
        console.print(header)

        # Show 3 example problems
        seen_problems: set[str] = set()
        shown = 0
        for ep in eps:
            problem = (ep.get("problem_description") or "")[:120].strip()
            problem_key = problem.lower()[:60]
            if problem_key in seen_problems or not problem:
                continue
            seen_problems.add(problem_key)
            console.print(f"  • {problem}")
            shown += 1
            if shown >= 3:
                break

        # If any have a fix summary, show one example fix
        for ep in eps:
            if ep.get("fix_summary"):
                console.print(f"  [dim]example fix:[/dim] {ep['fix_summary'][:160]}")
                break

        console.print()


# -----------------------------------------------------------------------------
# RECAP — what have I been working on recently
# -----------------------------------------------------------------------------

@app.command()
def recap(
    days: int = typer.Option(7, "--days", "-d", help="How far back to look"),
    limit: int = typer.Option(10, "--limit", "-n"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Filter by project id or name"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show what you've been working on recently — sessions + outcomes + latest context."""
    from datetime import datetime, timedelta, timezone

    store = _get_store(data_dir)

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Resolve project name to id if provided
    project_id = None
    if project:
        if project.startswith("p_"):
            project_id = project
        else:
            matches = store.sqlite.list_projects(keyword=project, limit=5)
            if matches:
                project_id = matches[0]["project_id"]

    sessions = store.sqlite.list_sessions(
        project_id=project_id,
        since=since,
        limit=limit * 2,
    )

    if not sessions:
        console.print(
            f"[yellow]No sessions in the last {days} days"
            + (f" for project '{project}'" if project else "")
            + ".[/yellow]"
        )
        return

    sessions = sessions[:limit]

    header_title = f"Recap — last {days} days"
    if project:
        header_title += f" · {project}"
    console.print(Panel.fit(f"[bold]{header_title}[/bold]", border_style="cyan"))
    console.print()

    for s in sessions:
        sid = s["session_id"]
        outcome = store.sqlite.get_outcome(sid)
        outcome_str = outcome["outcome"] if outcome else "—"
        outcome_color = {
            "shipped": "green",
            "fixed": "green",
            "stuck": "red",
            "abandoned": "dim",
            "exploratory": "blue",
        }.get(outcome_str, "white")

        # Get project info if available
        project_name = "—"
        if s.get("project_id"):
            proj = store.sqlite.get_project(s["project_id"])
            if proj:
                project_name = proj["display_name"]

        # Get the first real user message
        user_events = store.sqlite.get_events(
            session_id=sid, event_type="user_message", limit=3
        )
        first_user_msg = ""
        for ue in user_events:
            content = (ue.get("content") or "").strip()
            if content and not content.startswith("<"):
                first_user_msg = content[:150]
                break

        # Episode count for this session
        eps = store.sqlite.query_episodes(session_id=sid, limit=100)
        resolved = sum(1 for e in eps if e.get("status") == "resolved")

        # Header line
        header = Text()
        header.append(f"{_format_timestamp(s['started_at'])}  ", style="dim")
        header.append(f"{sid[:8]}  ", style="cyan")
        header.append(f"[{outcome_str}]", style=outcome_color)
        header.append(f"  {project_name}", style="magenta")
        if eps:
            header.append(f"  {resolved}/{len(eps)} episodes", style="yellow")
        console.print(header)

        if first_user_msg:
            console.print(f"  [dim]>[/dim] {first_user_msg}")

        if outcome and outcome.get("topics_json"):
            import json as _json
            try:
                topics = _json.loads(outcome["topics_json"])[:5]
                if topics:
                    console.print(f"  [dim]topics:[/dim] {', '.join(topics)}")
            except Exception:
                pass

        console.print()


# -----------------------------------------------------------------------------
# STATUS — project status with git context
# -----------------------------------------------------------------------------


@app.command("status")
def status_cmd(
    project: str = typer.Argument(..., help="Project name (fuzzy match)"),
    commits: int = typer.Option(10, "--commits", "-c", help="Max recent commits to show"),
    episodes: int = typer.Option(5, "--episodes", "-e", help="Max recent episodes"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show where a project left off — recent commits, issues, and context."""
    from longhand.recall.recall_pipeline import recall_project_status

    store = _get_store(data_dir)
    result = recall_project_status(
        store, project, max_commits=commits, max_episodes=episodes,
    )
    if not result:
        console.print(f"[red]No project matching: {project}[/red]")
        raise typer.Exit(1)

    console.print(Markdown(result.narrative))


# -----------------------------------------------------------------------------
# CONTINUE — pick up where you left off in a session
# -----------------------------------------------------------------------------

@app.command("continue")
def continue_cmd(
    session_id: str = typer.Argument(..., help="Session ID prefix"),
    n: int = typer.Option(10, "--events", "-n", help="How many recent events to show"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Show the last N events of a session so you can pick up where you left off."""
    store = _get_store(data_dir)

    sessions_rows = store.sqlite.list_sessions(limit=1000)
    full_id = None
    session_row = None
    for row in sessions_rows:
        if row["session_id"].startswith(session_id):
            full_id = row["session_id"]
            session_row = row
            break

    if not full_id or not session_row:
        console.print(f"[red]No session matching: {session_id}[/red]")
        raise typer.Exit(1)

    # Session header
    project_name = "—"
    if session_row.get("project_id"):
        proj = store.sqlite.get_project(session_row["project_id"])
        if proj:
            project_name = proj["display_name"]

    outcome = store.sqlite.get_outcome(full_id)

    header_lines = [
        f"[bold]{full_id}[/bold]",
        f"[dim]Project:[/dim] {project_name}",
        f"[dim]Started:[/dim] {_format_timestamp(session_row['started_at'])}",
        f"[dim]Ended:[/dim] {_format_timestamp(session_row['ended_at'])}",
        f"[dim]Events:[/dim] {session_row['event_count']}",
    ]
    if outcome:
        header_lines.append(f"[dim]Outcome:[/dim] {outcome['outcome']}")
    console.print(Panel.fit("\n".join(header_lines), title="Session", border_style="cyan"))
    console.print()

    # Grab all events then take the last n (excluding thinking for readability)
    all_events = store.sqlite.get_events(session_id=full_id, limit=10000)
    visible_events = [
        e for e in all_events
        if e["event_type"] not in ("assistant_thinking", "file_snapshot", "system")
    ]
    recent = visible_events[-n:]

    if not recent:
        console.print("[yellow]No visible events in session.[/yellow]")
        return

    console.print(f"[bold]Last {len(recent)} event(s):[/bold]\n")

    for e in recent:
        etype = e["event_type"]
        ts = _format_timestamp(e["timestamp"])
        marker = _event_marker(etype)

        content = (e.get("content") or "").strip()

        if etype == "user_message":
            console.print(f"[dim]{ts}[/dim] {marker} [bold blue]USER[/bold blue]")
            preview = content[:400]
            console.print(f"  {preview}")
        elif etype == "assistant_text":
            console.print(f"[dim]{ts}[/dim] {marker} [bold green]CLAUDE[/bold green]")
            preview = content[:400]
            console.print(f"  {preview}")
        elif etype == "tool_call":
            tool = e.get("tool_name") or "?"
            file_path = e.get("file_path") or ""
            desc = f"{tool}"
            if file_path:
                desc += f" {Path(file_path).name}"
            console.print(f"[dim]{ts}[/dim] {marker} [magenta]{desc}[/magenta]")
        elif etype == "tool_result":
            status = "[red]error[/red]" if e.get("error_detected") else "[green]ok[/green]"
            console.print(f"[dim]{ts}[/dim] {marker} {status}")
            if e.get("error_detected") and e.get("error_snippet"):
                console.print(f"  [red]{e['error_snippet'][:200]}[/red]")

    console.print()

    # Last unresolved question or open episode
    unresolved_eps = store.sqlite.query_episodes(
        session_id=full_id, status="unresolved", limit=5
    )
    if unresolved_eps:
        console.print(
            Panel.fit(
                f"[yellow]⚠ {len(unresolved_eps)} unresolved episode(s) in this session[/yellow]\n"
                + "\n".join(
                    f"  • {(ep.get('problem_description') or '')[:100]}"
                    for ep in unresolved_eps[:3]
                ),
                border_style="yellow",
            )
        )


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


# Prompt hook subcommands — auto-context injection on user message
prompt_hook_app = typer.Typer(
    name="prompt-hook",
    help="Claude Code UserPromptSubmit hook (auto-context injection)",
)
app.add_typer(prompt_hook_app, name="prompt-hook")


@prompt_hook_app.command("install")
def prompt_hook_install_cmd():
    """Install the UserPromptSubmit hook for auto-context injection."""
    _prompt_hook_install()


@prompt_hook_app.command("uninstall")
def prompt_hook_uninstall_cmd():
    """Remove the UserPromptSubmit hook."""
    _prompt_hook_uninstall()


@app.command("__prompt-hook-run", hidden=True)
def prompt_hook_run_cmd():
    """Internal: read stdin JSON, return additionalContext via hookSpecificOutput.

    Called by the UserPromptSubmit hook wiring. Not for direct user invocation.
    """
    _run_prompt_hook()


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
    transcript: Optional[str] = typer.Option(None, "--transcript", "-t", help="Path to a single session JSONL"),
    data_dir: Optional[str] = typer.Option(None, "--data-dir"),
):
    """Ingest a single session file.

    Dual-mode: accepts `--transcript <path>` for direct CLI use, OR reads
    `{"transcript_path": "..."}` JSON from stdin when invoked by Claude Code's
    SessionEnd hook (modern Claude Code passes hook data via stdin, not env vars).
    """
    if not transcript:
        import json as _json
        import sys as _sys

        try:
            raw = _sys.stdin.read(262144)  # bounded, ≤256KB
            if raw.strip():
                data = _json.loads(raw)
                if isinstance(data, dict):
                    transcript = data.get("transcript_path") or data.get("transcript") or None
        except Exception:
            pass

        if not transcript:
            # Hook invoked with no transcript info — exit silently so we never
            # crash the Claude Code hook chain.
            return

    _ingest_single(transcript, data_dir)


@app.command()
def doctor():
    """Diagnose Longhand installation and data."""
    _doctor()


if __name__ == "__main__":
    app()
