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

def _wrap_hook_command(command: str, matcher: str = "") -> dict:
    """Build a hook entry in Claude Code's expected schema.

    Claude Code requires:
        {
            "matcher": "<tool name or empty>",
            "hooks": [
                {"type": "command", "command": "..."}
            ]
        }
    """
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command}],
    }


def _entry_contains_command(entry: dict, needle: str) -> bool:
    """Check if a hook entry (in either flat or wrapped format) contains a command substring."""
    if not isinstance(entry, dict):
        return False
    # Wrapped format
    inner = entry.get("hooks")
    if isinstance(inner, list):
        for h in inner:
            if isinstance(h, dict) and needle in (h.get("command") or ""):
                return True
    # Flat (legacy) format
    if needle in (entry.get("command") or ""):
        return True
    return False


def _hook_command_is_stale(entry: dict) -> bool:
    """Detect the v≤0.5.1 SessionEnd hook command that relied on the
    `$CLAUDE_TRANSCRIPT_PATH` env var. Modern Claude Code passes hook data via
    stdin JSON, so that command silently fails on every session end.
    """
    inner = entry.get("hooks")
    if isinstance(inner, list):
        for h in inner:
            cmd = (h.get("command") if isinstance(h, dict) else "") or ""
            if "longhand ingest-session" in cmd and "$CLAUDE_TRANSCRIPT_PATH" in cmd:
                return True
    cmd = entry.get("command") or ""
    if "longhand ingest-session" in cmd and "$CLAUDE_TRANSCRIPT_PATH" in cmd:
        return True
    return False


def hook_install() -> None:
    """Install (or auto-upgrade) the SessionEnd hook in ~/.claude/settings.json."""
    settings = _load_json(CLAUDE_SETTINGS_PATH)
    backup = _backup(CLAUDE_SETTINGS_PATH)

    longhand_bin = shutil.which("longhand") or f"{sys.executable} -m longhand.cli"
    # Bare command — `ingest-session` reads `transcript_path` from stdin JSON
    # as of v0.5.2 (Claude Code changed from env var to stdin JSON payload).
    command = f"{longhand_bin} ingest-session"

    hooks = settings.setdefault("hooks", {})
    session_end = hooks.setdefault("SessionEnd", [])

    # Auto-upgrade stale pre-0.5.2 hooks that still reference
    # $CLAUDE_TRANSCRIPT_PATH (which modern Claude Code never sets).
    upgraded = 0
    for entry in session_end:
        if _hook_command_is_stale(entry):
            inner = entry.get("hooks")
            if isinstance(inner, list):
                for h in inner:
                    if isinstance(h, dict) and "$CLAUDE_TRANSCRIPT_PATH" in (h.get("command") or ""):
                        h["command"] = command
                        upgraded += 1
            elif "$CLAUDE_TRANSCRIPT_PATH" in (entry.get("command") or ""):
                entry["command"] = command
                upgraded += 1

    if upgraded:
        _save_json(CLAUDE_SETTINGS_PATH, settings)
        console.print(
            Panel.fit(
                f"[green]✓[/green] Upgraded {upgraded} stale SessionEnd hook(s)\n"
                f"[dim]Config:[/dim] {CLAUDE_SETTINGS_PATH}\n"
                f"[dim]Backup:[/dim] {backup or 'n/a'}\n\n"
                f"Your hook was written for an older Claude Code API "
                f"(env var [bold]$CLAUDE_TRANSCRIPT_PATH[/bold]) and had been\n"
                f"silently failing on every session end. Now reads stdin JSON.",
                title="Hook upgraded",
                border_style="green",
            )
        )
        return

    if any(_entry_contains_command(h, "longhand ingest-session") for h in session_end):
        console.print("[yellow]Longhand SessionEnd hook already installed.[/yellow]")
        return

    session_end.append(_wrap_hook_command(command))
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


def prompt_hook_install() -> None:
    """Install the UserPromptSubmit hook for auto-context injection."""
    settings = _load_json(CLAUDE_SETTINGS_PATH)
    backup = _backup(CLAUDE_SETTINGS_PATH)

    longhand_bin = shutil.which("longhand") or f"{sys.executable} -m longhand.cli"
    command = f"{longhand_bin} __prompt-hook-run"

    hooks = settings.setdefault("hooks", {})
    user_prompt = hooks.setdefault("UserPromptSubmit", [])

    if any(_entry_contains_command(h, "__prompt-hook-run") for h in user_prompt):
        console.print("[yellow]Longhand prompt hook already installed.[/yellow]")
        return

    user_prompt.append(_wrap_hook_command(command))
    _save_json(CLAUDE_SETTINGS_PATH, settings)

    console.print(
        Panel.fit(
            f"[green]✓[/green] Installed UserPromptSubmit hook\n"
            f"[dim]Config:[/dim] {CLAUDE_SETTINGS_PATH}\n"
            f"[dim]Backup:[/dim] {backup or 'n/a'}\n\n"
            f"Longhand will now auto-inject relevant past context into your "
            f"Claude Code prompts when there's a strong match. Quiet by default —\n"
            f"only injects when relevance is high.",
            title="Prompt hook installed",
            border_style="green",
        )
    )


def prompt_hook_uninstall() -> None:
    """Remove the UserPromptSubmit hook."""
    if not CLAUDE_SETTINGS_PATH.exists():
        console.print("[yellow]No Claude settings file found.[/yellow]")
        return

    settings = _load_json(CLAUDE_SETTINGS_PATH)
    hooks = settings.get("hooks", {})
    user_prompt = hooks.get("UserPromptSubmit", [])

    filtered = [
        h for h in user_prompt
        if not _entry_contains_command(h, "__prompt-hook-run")
    ]

    if len(filtered) == len(user_prompt):
        console.print("[yellow]Longhand prompt hook was not installed.[/yellow]")
        return

    if filtered:
        hooks["UserPromptSubmit"] = filtered
    else:
        hooks.pop("UserPromptSubmit", None)

    _backup(CLAUDE_SETTINGS_PATH)
    _save_json(CLAUDE_SETTINGS_PATH, settings)
    console.print("[green]✓[/green] Removed UserPromptSubmit hook")


_HOOK_STDIN_MAX_BYTES = 256 * 1024  # 256KB — Claude Code prompts never exceed this
_HOOK_PROMPT_MAX_LEN = 8000          # Cap the prompt we pass to the recall pipeline

# Configurable hook behavior — users can override via ~/.longhand/config.json
_DEFAULT_HOOK_CONFIG = {
    "min_relevance": 2.5,        # Minimum relevance score to inject (higher = less noise)
    "max_inject_chars": 2000,    # Max characters injected into prompt
    "max_episodes": 2,           # Max episodes to consider
    "enabled": True,             # Set to false to disable without uninstalling
}


def _load_hook_config() -> dict:
    """Load hook configuration from ~/.longhand/config.json, falling back to defaults."""
    import json as _json
    config_path = Path.home() / ".longhand" / "config.json"
    config = dict(_DEFAULT_HOOK_CONFIG)
    try:
        if config_path.exists():
            user = _json.loads(config_path.read_text())
            if isinstance(user, dict) and "hook" in user:
                for k, v in user["hook"].items():
                    if k in config:
                        config[k] = v
    except Exception:
        pass
    return config


def run_prompt_hook() -> None:
    """Read stdin JSON, run longhand context, and emit hookSpecificOutput JSON.

    This is what the hook actually invokes. Designed to be silent and fail-safe
    so it can never break Claude Code.
    """
    import io as _io
    import json as _json

    try:
        # Bounded read — never accept more than 256KB of stdin
        raw = sys.stdin.read(_HOOK_STDIN_MAX_BYTES + 1)
        if len(raw) > _HOOK_STDIN_MAX_BYTES:
            print("{}")
            return
        if not raw.strip():
            print("{}")
            return

        data = _json.loads(raw)
        if not isinstance(data, dict):
            print("{}")
            return

        prompt = data.get("prompt") or ""

        if not isinstance(prompt, str) or len(prompt.strip()) < 12:
            print("{}")
            return

        # Load user-configurable hook settings
        hook_config = _load_hook_config()

        if not hook_config.get("enabled", True):
            print("{}")
            return

        # Cap prompt length before recall — protects against pathological queries
        if len(prompt) > _HOOK_PROMPT_MAX_LEN:
            prompt = prompt[:_HOOK_PROMPT_MAX_LEN]

        # Capture context output
        import contextlib

        from longhand.cli import context as context_cmd

        captured = _io.StringIO()
        with contextlib.redirect_stdout(captured):
            try:
                context_cmd(
                    query=prompt,
                    max_episodes=hook_config.get("max_episodes", 2),
                    min_relevance=hook_config.get("min_relevance", 2.5),
                    project=None,
                    silent_if_empty=True,
                    data_dir=None,
                )
            except SystemExit:
                pass
            except Exception:
                pass

        injected = captured.getvalue().strip()

        # Cap injection size to limit token usage
        max_inject = hook_config.get("max_inject_chars", 2000)
        if len(injected) > max_inject:
            injected = injected[:max_inject] + "\n[... capped at " + str(max_inject) + " chars]"

        if not injected:
            print("{}")
            return

        # Emit hookSpecificOutput JSON for Claude Code to inject
        output = {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": injected,
            }
        }
        print(_json.dumps(output))
    except Exception:
        # Hooks must NEVER crash the parent process
        print("{}")


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
        if not _entry_contains_command(h, "longhand ingest-session")
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

def ingest_single_session(transcript: str, data_dir: str | None = None) -> None:
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
        raise typer.Exit(1) from e


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
            "[yellow]⚠[/yellow] not on PATH (run [bold]pip install longhand[/bold])",
        )

    # 2. SessionEnd hook installed?
    hook_installed = False
    hook_stale = False
    prompt_hook_installed = False
    if CLAUDE_SETTINGS_PATH.exists():
        settings = _load_json(CLAUDE_SETTINGS_PATH)
        for h in settings.get("hooks", {}).get("SessionEnd", []):
            if _entry_contains_command(h, "longhand ingest-session"):
                hook_installed = True
                if _hook_command_is_stale(h):
                    hook_stale = True
                break
        for h in settings.get("hooks", {}).get("UserPromptSubmit", []):
            if _entry_contains_command(h, "__prompt-hook-run"):
                prompt_hook_installed = True
                break

    if hook_stale:
        table.add_row(
            "SessionEnd hook",
            "[red]✗[/red] stale — uses pre-0.5.2 $CLAUDE_TRANSCRIPT_PATH "
            "(silently failing). Run [bold]longhand hook install[/bold] to upgrade.",
        )
    elif hook_installed:
        table.add_row("SessionEnd hook", "[green]✓[/green] installed (auto-ingest)")
    else:
        table.add_row(
            "SessionEnd hook",
            "[yellow]⚠[/yellow] not installed (run [bold]longhand hook install[/bold])",
        )

    if prompt_hook_installed:
        table.add_row("UserPromptSubmit hook", "[green]✓[/green] installed (auto-context)")
    else:
        table.add_row(
            "UserPromptSubmit hook",
            "[yellow]⚠[/yellow] not installed (run [bold]longhand prompt-hook install[/bold])",
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
            "[dim]—[/dim] not installed (run [bold]longhand mcp install[/bold])",
        )

    # 4. Claude Code MCP installed?
    cc_mcp_installed = False
    cc_config = Path.home() / ".claude.json"
    if cc_config.exists():
        try:
            cc_data = _load_json(cc_config)
            servers = cc_data.get("mcpServers", {})
            if "longhand" in servers:
                cc_mcp_installed = True
        except Exception:
            pass

    if cc_mcp_installed:
        table.add_row("Claude Code MCP", "[green]✓[/green] installed")
    else:
        table.add_row(
            "Claude Code MCP",
            "[dim]—[/dim] not installed (run [bold]claude mcp add longhand -s user -- longhand mcp-server[/bold])",
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
