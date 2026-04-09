"""
Setup/install commands for Longhand.

Wires Longhand into Claude Code (hook) and Claude Desktop (MCP), provides
a single-session ingest command for the hook to call, and a doctor command
for diagnostics.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from longhand.parser import JSONLParser
from longhand.storage import LonghandStore


console = Console()


CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_DESKTOP_CONFIG_PATH = (
    Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".longhand-backup")
    shutil.copy2(path, backup)
    return backup


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


# ─── Hook install ──────────────────────────────────────────────────────────

def hook_install() -> None:
    """Install the SessionEnd hook into ~/.claude/settings.json."""
    settings = _load_json(CLAUDE_SETTINGS_PATH)
    backup = _backup(CLAUDE_SETTINGS_PATH)

    longhand_bin = shutil.which("longhand") or f"{sys.executable} -m longhand.cli"

    hook_entry = {
        "command": f"{longhand_bin} ingest-session --transcript \"$CLAUDE_TRANSCRIPT_PATH\""
    }

    hooks = settings.setdefault("hooks", {})
    session_end = hooks.setdefault("SessionEnd", [])

    # Avoid duplicates
    already = any(
        isinstance(h, dict) and "longhand ingest-session" in h.get("command", "")
        for h in session_end
    )
    if already:
        console.print("[yellow]Longhand SessionEnd hook already installed.[/yellow]")
        return

    session_end.append(hook_entry)
    _save_json(CLAUDE_SETTINGS_PATH, settings)

    console.print(
        Panel.fit(
            f"[green]✓[/green] Installed SessionEnd hook\n"
            f"[dim]Config:[/dim] {CLAUDE_SETTINGS_PATH}\n"
            f"[dim]Backup:[/dim] {backup or 'n/a'}\n\n"
            f"Longhand will now auto-ingest every Claude Code session when it ends.",
            title="Hook installed",
            border_style="green",
        )
    )


def hook_uninstall() -> None:
    """Remove the SessionEnd hook from ~/.claude/settings.json."""
    if not CLAUDE_SETTINGS_PATH.exists():
        console.print("[yellow]No Claude settings file found.[/yellow]")
        return

    settings = _load_json(CLAUDE_SETTINGS_PATH)
    hooks = settings.get("hooks", {})
    session_end = hooks.get("SessionEnd", [])

    filtered = [
        h for h in session_end
        if not (isinstance(h, dict) and "longhand ingest-session" in h.get("command", ""))
    ]

    if len(filtered) == len(session_end):
        console.print("[yellow]Longhand SessionEnd hook was not installed.[/yellow]")
        return

    if filtered:
        hooks["SessionEnd"] = filtered
    else:
        hooks.pop("SessionEnd", None)

    _backup(CLAUDE_SETTINGS_PATH)
    _save_json(CLAUDE_SETTINGS_PATH, settings)
    console.print("[green]✓[/green] Removed SessionEnd hook")


# ─── MCP install ───────────────────────────────────────────────────────────

def mcp_install() -> None:
    """Install Longhand's MCP server into Claude Desktop config."""
    config = _load_json(CLAUDE_DESKTOP_CONFIG_PATH)
    backup = _backup(CLAUDE_DESKTOP_CONFIG_PATH)

    longhand_bin = shutil.which("longhand") or sys.executable
    args = ["-m", "longhand.mcp_server"] if longhand_bin == sys.executable else ["mcp-server"]

    mcp_servers = config.setdefault("mcpServers", {})
    mcp_servers["longhand"] = {
        "command": longhand_bin,
        "args": args,
    }

    _save_json(CLAUDE_DESKTOP_CONFIG_PATH, config)

    console.print(
        Panel.fit(
            f"[green]✓[/green] Installed Longhand MCP server\n"
            f"[dim]Config:[/dim] {CLAUDE_DESKTOP_CONFIG_PATH}\n"
            f"[dim]Backup:[/dim] {backup or 'n/a'}\n\n"
            f"Restart Claude Desktop to activate. After restart, Claude will have "
            f"access to [bold]recall[/bold], [bold]match_project[/bold], "
            f"[bold]find_episodes[/bold], and more.",
            title="MCP installed",
            border_style="green",
        )
    )


def mcp_uninstall() -> None:
    """Remove Longhand's MCP server from Claude Desktop config."""
    if not CLAUDE_DESKTOP_CONFIG_PATH.exists():
        console.print("[yellow]No Claude Desktop config found.[/yellow]")
        return

    config = _load_json(CLAUDE_DESKTOP_CONFIG_PATH)
    servers = config.get("mcpServers", {})
    if "longhand" not in servers:
        console.print("[yellow]Longhand MCP server was not installed.[/yellow]")
        return

    _backup(CLAUDE_DESKTOP_CONFIG_PATH)
    servers.pop("longhand", None)
    _save_json(CLAUDE_DESKTOP_CONFIG_PATH, config)
    console.print("[green]✓[/green] Removed Longhand MCP server")


# ─── Ingest single session (for hook) ──────────────────────────────────────

def ingest_single_session(transcript: str, data_dir: Optional[str] = None) -> None:
    """Ingest a single Claude Code JSONL file with full analysis.

    Called by the SessionEnd hook. Non-blocking, fast (~1-2s).
    """
    path = Path(transcript).expanduser()
    if not path.exists():
        console.print(f"[red]Transcript not found: {transcript}[/red]")
        raise typer.Exit(1)

    store = LonghandStore(data_dir=data_dir)
    try:
        parser = JSONLParser(path)
        events = list(parser.parse_events())
        if not events:
            console.print(f"[yellow]No events in {path.name}[/yellow]")
            return
        session = parser.build_session(events)
        result = store.ingest_session(session, events, run_analysis=True)
        console.print(
            f"[green]✓[/green] Ingested {session.session_id[:8]} — "
            f"{result['events_stored']} events, "
            f"{result['episodes']} episodes"
        )
    except Exception as e:
        console.print(f"[red]✗[/red] Failed to ingest {path.name}: {e}")
        raise typer.Exit(1)


# ─── Doctor ────────────────────────────────────────────────────────────────

def doctor() -> None:
    """Diagnose Longhand installation and data."""
    table = Table(title="Longhand Doctor", show_header=False, border_style="cyan")
    table.add_column("Check", style="bold")
    table.add_column("Status")

    # 1. longhand on PATH?
    longhand_bin = shutil.which("longhand")
    if longhand_bin:
        table.add_row("longhand CLI", f"[green]✓[/green] {longhand_bin}")
    else:
        table.add_row(
            "longhand CLI",
            "[yellow]⚠[/yellow] not on PATH (run [bold]pip install -e .[/bold])",
        )

    # 2. SessionEnd hook installed?
    hook_installed = False
    if CLAUDE_SETTINGS_PATH.exists():
        settings = _load_json(CLAUDE_SETTINGS_PATH)
        for h in settings.get("hooks", {}).get("SessionEnd", []):
            if isinstance(h, dict) and "longhand ingest-session" in h.get("command", ""):
                hook_installed = True
                break

    if hook_installed:
        table.add_row("SessionEnd hook", "[green]✓[/green] installed")
    else:
        table.add_row(
            "SessionEnd hook",
            "[yellow]⚠[/yellow] not installed (run [bold]longhand hook install[/bold])",
        )

    # 3. Claude Desktop MCP installed?
    mcp_installed = False
    if CLAUDE_DESKTOP_CONFIG_PATH.exists():
        config = _load_json(CLAUDE_DESKTOP_CONFIG_PATH)
        if "longhand" in config.get("mcpServers", {}):
            mcp_installed = True

    if mcp_installed:
        table.add_row("Claude Desktop MCP", "[green]✓[/green] installed")
    else:
        table.add_row(
            "Claude Desktop MCP",
            "[yellow]⚠[/yellow] not installed (run [bold]longhand mcp install[/bold])",
        )

    # 4. Data directory
    store = LonghandStore()
    data_ok = store.data_dir.exists() and store.data_dir.is_dir()
    table.add_row(
        "Data directory",
        f"[green]✓[/green] {store.data_dir}" if data_ok else f"[red]✗[/red] {store.data_dir}",
    )

    # 5. Stats
    stats = store.stats()
    table.add_row("Sessions ingested", f"{stats.get('sessions', 0):,}")
    table.add_row("Events stored", f"{stats.get('events', 0):,}")
    table.add_row("Projects inferred", f"{stats.get('projects', 0):,}")
    table.add_row("Episodes extracted", f"{stats.get('episodes', 0):,}")

    sessions_needing_analysis = max(
        0, stats.get("sessions", 0) - stats.get("outcomes", 0)
    )
    if sessions_needing_analysis > 0:
        table.add_row(
            "Sessions needing analysis",
            f"[yellow]{sessions_needing_analysis}[/yellow] (run [bold]longhand analyze --all[/bold])",
        )

    console.print(table)
