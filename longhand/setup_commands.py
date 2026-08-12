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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from longhand.parser import JSONLParser, discover_sessions
from longhand.storage import LonghandStore
from longhand.timeutil import utcnow

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
    """Install/upgrade the SessionEnd + Stop hooks in ~/.claude/settings.json.

    SessionEnd runs full ingest with analysis when a session closes. Stop
    fires once per assistant turn and runs the cheap live-tail ingest, so
    in-progress sessions are queryable and crashes don't lose work.
    """
    settings = _load_json(CLAUDE_SETTINGS_PATH)
    backup = _backup(CLAUDE_SETTINGS_PATH)

    longhand_bin = shutil.which("longhand") or f"{sys.executable} -m longhand"
    # Bare commands — both read `transcript_path` from stdin JSON.
    session_end_cmd = f"{longhand_bin} ingest-session"
    stop_cmd = f"{longhand_bin} ingest-live"

    hooks = settings.setdefault("hooks", {})
    session_end = hooks.setdefault("SessionEnd", [])
    stop = hooks.setdefault("Stop", [])

    # Auto-upgrade stale pre-0.5.2 SessionEnd hooks that still reference
    # $CLAUDE_TRANSCRIPT_PATH (which modern Claude Code never sets).
    upgraded = 0
    for entry in session_end:
        if _hook_command_is_stale(entry):
            inner = entry.get("hooks")
            if isinstance(inner, list):
                for h in inner:
                    if isinstance(h, dict) and "$CLAUDE_TRANSCRIPT_PATH" in (
                        h.get("command") or ""
                    ):
                        h["command"] = session_end_cmd
                        upgraded += 1
            elif "$CLAUDE_TRANSCRIPT_PATH" in (entry.get("command") or ""):
                entry["command"] = session_end_cmd
                upgraded += 1

    session_end_present = any(
        _entry_contains_command(h, "longhand ingest-session") for h in session_end
    )
    stop_present = any(_entry_contains_command(h, "longhand ingest-live") for h in stop)

    added: list[str] = []
    if not session_end_present:
        session_end.append(_wrap_hook_command(session_end_cmd))
        added.append("SessionEnd")
    if not stop_present:
        stop.append(_wrap_hook_command(stop_cmd))
        added.append("Stop")

    if not added and not upgraded:
        console.print("[yellow]Longhand hooks already installed.[/yellow]")
        return

    _save_json(CLAUDE_SETTINGS_PATH, settings)

    if upgraded and not added:
        console.print(
            Panel.fit(
                f"[green]✓[/green] Upgraded {upgraded} stale SessionEnd hook(s)\n"
                f"[dim]Config:[/dim] {CLAUDE_SETTINGS_PATH}\n"
                f"[dim]Backup:[/dim] {backup or 'n/a'}",
                title="Hook upgraded",
                border_style="green",
            )
        )
        return

    body = []
    if added:
        body.append(f"[green]✓[/green] Installed hook(s): [bold]{', '.join(added)}[/bold]")
    if upgraded:
        body.append(f"[green]✓[/green] Upgraded {upgraded} stale SessionEnd hook(s)")
    body.append(f"[dim]Config:[/dim] {CLAUDE_SETTINGS_PATH}")
    body.append(f"[dim]Backup:[/dim] {backup or 'n/a'}")
    body.append("")
    body.append(
        "SessionEnd runs the full ingest. Stop runs a cheap live-tail every\n"
        "assistant turn so in-progress sessions stay queryable."
    )

    console.print(Panel.fit("\n".join(body), title="Hook installed", border_style="green"))


def prompt_hook_install() -> None:
    """Install the UserPromptSubmit hook for auto-context injection."""
    settings = _load_json(CLAUDE_SETTINGS_PATH)
    backup = _backup(CLAUDE_SETTINGS_PATH)

    longhand_bin = shutil.which("longhand") or f"{sys.executable} -m longhand"
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

    filtered = [h for h in user_prompt if not _entry_contains_command(h, "__prompt-hook-run")]

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
_HOOK_PROMPT_MAX_LEN = 8000  # Cap the prompt we pass to the recall pipeline

# Configurable hook behavior — users can override via ~/.longhand/config.json
_DEFAULT_HOOK_CONFIG = {
    "min_relevance": 2.5,  # Minimum relevance score to inject (higher = less noise)
    "max_inject_chars": 2000,  # Max characters injected into prompt
    "max_episodes": 2,  # Max episodes to consider
    "enabled": True,  # Set to false to disable without uninstalling
}


def _load_hook_config() -> dict:
    """Load hook configuration from ~/.longhand/config.json, falling back to defaults."""
    import json as _json

    from longhand.storage.store import resolve_data_dir

    config_path = resolve_data_dir() / "config.json"
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

        from longhand.cli._commands import context as context_cmd

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
    """Remove the SessionEnd and Stop hooks from ~/.claude/settings.json."""
    if not CLAUDE_SETTINGS_PATH.exists():
        console.print("[yellow]No Claude settings file found.[/yellow]")
        return

    settings = _load_json(CLAUDE_SETTINGS_PATH)
    hooks = settings.get("hooks", {})

    removed: list[str] = []

    session_end = hooks.get("SessionEnd", [])
    filtered = [h for h in session_end if not _entry_contains_command(h, "longhand ingest-session")]
    if len(filtered) != len(session_end):
        if filtered:
            hooks["SessionEnd"] = filtered
        else:
            hooks.pop("SessionEnd", None)
        removed.append("SessionEnd")

    stop = hooks.get("Stop", [])
    filtered_stop = [h for h in stop if not _entry_contains_command(h, "longhand ingest-live")]
    if len(filtered_stop) != len(stop):
        if filtered_stop:
            hooks["Stop"] = filtered_stop
        else:
            hooks.pop("Stop", None)
        removed.append("Stop")

    if not removed:
        console.print("[yellow]Longhand hooks were not installed.[/yellow]")
        return

    _backup(CLAUDE_SETTINGS_PATH)
    _save_json(CLAUDE_SETTINGS_PATH, settings)
    console.print(f"[green]✓[/green] Removed hook(s): {', '.join(removed)}")


# ─── Reconciler launchd install ───────────────────────────────────────────

RECONCILER_PLIST_LABEL = "com.longhand.reconcile"
RECONCILER_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{RECONCILER_PLIST_LABEL}.plist"
RECONCILER_INTERVAL_SECONDS = 30 * 60  # 30 minutes


def _reconciler_plist_xml(longhand_bin: str, log_path: Path) -> str:
    # launchd does not inherit shell env: bake the resolved data dir in, or a
    # LONGHAND_DATA_DIR-relocated store would be invisible to the reconciler.
    from longhand.storage.store import resolve_data_dir

    data_dir = resolve_data_dir()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{RECONCILER_PLIST_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{longhand_bin}</string>
        <string>reconcile</string>
        <string>--fix</string>
    </array>
    <key>StartInterval</key>
    <integer>{RECONCILER_INTERVAL_SECONDS}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>ProcessType</key>
    <string>Background</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LONGHAND_DATA_DIR</key>
        <string>{data_dir}</string>
    </dict>
</dict>
</plist>
"""


def schedule_install_reconciler() -> None:
    """Install a launchd job that runs ``longhand reconcile --fix`` every 30 minutes.

    Belt-and-suspenders for the silent-crash failure mode: if Claude Code
    crashes hard enough that neither the Stop hook nor SessionEnd fired,
    the next reconciler tick will catch up the missing transcripts.

    macOS-only for now (uses launchd). Idempotent: re-running overwrites
    the plist with the current longhand binary path.
    """
    if sys.platform != "darwin":
        console.print(
            "[yellow]Reconciler installer is macOS-only. "
            "On Linux, set up a cron/systemd-user job for `longhand reconcile --fix`.[/yellow]"
        )
        return

    longhand_bin = shutil.which("longhand")
    if not longhand_bin:
        console.print(
            "[red]✗[/red] `longhand` not on PATH — install it first "
            "([bold]pip install longhand[/bold])."
        )
        raise typer.Exit(1)

    from longhand.storage.store import resolve_data_dir as _resolve

    logs_dir = _resolve() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_path = logs_dir / "reconcile.log"

    RECONCILER_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECONCILER_PLIST_PATH.write_text(_reconciler_plist_xml(longhand_bin, log_path))

    # Reload via launchctl so it picks up the new/updated plist immediately.
    import subprocess as _sp

    try:
        _sp.run(
            ["launchctl", "unload", str(RECONCILER_PLIST_PATH)],
            check=False,
            capture_output=True,
        )
        load = _sp.run(
            ["launchctl", "load", str(RECONCILER_PLIST_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        load = None

    body = [
        f"[green]✓[/green] Installed reconciler at [bold]{RECONCILER_PLIST_PATH}[/bold]",
        f"[dim]Runs:[/dim] {longhand_bin} reconcile --fix",
        f"[dim]Interval:[/dim] every {RECONCILER_INTERVAL_SECONDS // 60} minutes",
        f"[dim]Log:[/dim] {log_path}",
    ]
    if load is not None and load.returncode != 0 and load.stderr:
        body.append(f"[yellow]⚠[/yellow] launchctl load: {load.stderr.strip()}")

    console.print(Panel.fit("\n".join(body), title="Reconciler installed", border_style="green"))


def schedule_uninstall_reconciler() -> None:
    """Remove the launchd reconciler job."""
    if not RECONCILER_PLIST_PATH.exists():
        console.print("[yellow]Reconciler was not installed.[/yellow]")
        return

    import subprocess as _sp

    try:
        _sp.run(
            ["launchctl", "unload", str(RECONCILER_PLIST_PATH)],
            check=False,
            capture_output=True,
        )
    except FileNotFoundError:
        pass

    try:
        RECONCILER_PLIST_PATH.unlink()
    except OSError as e:
        console.print(f"[red]✗[/red] Could not remove plist: {e}")
        raise typer.Exit(1) from e

    console.print("[green]✓[/green] Removed reconciler launchd job")


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
            f"access to [bold]recall[/bold], [bold]recall_project_status[/bold], "
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


def _hook_data_dir(data_dir: str | None) -> Path:
    """LonghandStore's data-dir choice without constructing a store —
    store-open failures still need somewhere to leave a breadcrumb."""
    from longhand.storage.store import resolve_data_dir

    return resolve_data_dir(data_dir)


def _log_hook_error(data_dir: Path, message: str) -> None:
    """Append one line to the daily hook-error log. Never raises — the log is
    best-effort breadcrumbs for doctor, not another way for a hook to fail."""
    try:
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = datetime.now(timezone.utc)
        line = f"{now.isoformat(timespec='seconds')} {message}\n"
        with (logs / f"hook-errors-{now.strftime('%Y-%m-%d')}.log").open("a") as fh:
            fh.write(line)
    except Exception:
        pass


def ingest_single_session(
    transcript: str,
    data_dir: str | None = None,
    run_analysis: bool = True,
    hook_mode: bool = False,
) -> None:
    """Ingest a single Claude Code JSONL file.

    Called by the SessionEnd hook (``hook_mode=True``, transcript from stdin)
    and by humans passing an explicit ``--transcript`` (``hook_mode=False``).
    Non-blocking, fast (~1-2s) when analysis runs; even faster when skipped.
    Pass ``run_analysis=False`` to populate SQLite only (no episodes,
    segments, or vectors). Power users can defer the analysis pass via
    ``longhand analyze --all``.

    Hook mode never exits nonzero: a failing hook must not crash the Claude
    Code hook chain, so every failure becomes a one-line stderr note plus a
    breadcrumb in ``logs/hook-errors-YYYY-MM-DD.log`` (surfaced by ``longhand
    doctor``), and the transcript is left for reconcile to heal. Explicit CLI
    use keeps loud exit-1 failures — human misuse deserves an error.

    Takes the ingest lock: parallel sessions ending together would otherwise
    open concurrent ChromaDB writers on the same directory, which ChromaDB
    does not tolerate. A locked-out session defers (success exit) — the bulk
    ingest holding the lock, or a later reconcile, picks it up.
    """
    from longhand.recall.project_fallback import (
        claim_ingest_lock,
        release_ingest_lock,
    )

    def _hook_fail(bucket: str, detail: str) -> None:
        """One stderr line + one log breadcrumb; the caller returns exit-0."""
        line = f"ingest-session {bucket}: {detail}"
        print(f"longhand: {line}", file=sys.stderr)
        _log_hook_error(_hook_data_dir(data_dir), line)

    path = Path(transcript).expanduser()
    if not path.exists():
        if hook_mode:
            _hook_fail("missing-transcript", str(path))
            return
        console.print(f"[red]Transcript not found: {transcript}[/red]")
        raise typer.Exit(1)

    try:
        store = LonghandStore(data_dir=data_dir)
    except Exception as e:
        if hook_mode:
            _hook_fail("store-open-failed", f"{type(e).__name__}: {e}")
            return
        console.print(f"[red]✗[/red] Could not open the Longhand store: {e}")
        raise typer.Exit(1) from e

    if not claim_ingest_lock(store):
        console.print(
            f"[yellow]Another Longhand ingest is running — deferring {path.name}; "
            "reconcile will pick it up.[/yellow]"
        )
        return

    try:
        try:
            # The parser constructor enforces the oversize cap (ValueError).
            # Distinct bucket: reconcile books these under skipped_oversize,
            # and the breadcrumb should say why the session never appears.
            parser = JSONLParser(path)
        except ValueError as e:
            if hook_mode:
                _hook_fail("oversize-transcript", str(e))
                return
            console.print(f"[red]✗[/red] {e}")
            raise typer.Exit(1) from e
        events = list(parser.parse_events())
        if not events:
            console.print(f"[yellow]No events in {path.name}[/yellow]")
            return
        session = parser.build_session(events)
        result = store.ingest_session(session, events, run_analysis=run_analysis)
        console.print(
            f"[green]✓[/green] Ingested {session.session_id[:8]} — "
            f"{result['events_stored']} events, "
            f"{result['episodes']} episodes"
        )
    except typer.Exit:
        raise
    except Exception as e:
        if hook_mode:
            _hook_fail("ingest-failed", f"{path.name}: {type(e).__name__}: {e}")
            return
        console.print(f"[red]✗[/red] Failed to ingest {path.name}: {e}")
        raise typer.Exit(1) from e
    finally:
        release_ingest_lock(store)


# ─── Live ingest (for Stop hook) ──────────────────────────────────────────


def _live_best_project_cwd(store: LonghandStore, session_id: str) -> str | None:
    """Most-common event cwd for a session that resolves to a real project root
    ($HOME and marker-less dirs excluded) — the same signal `build_session` uses.

    SQL-driven and bounded so it stays cheap in the per-turn Stop hook, and never
    raises. Returns None when no project cwd is found, in which case the caller
    preserves the existing cwd rather than overwriting it. Replaces the old
    `MAX(cwd)` pick, which chose a meaningless lexicographic max and drifted cwd
    out of sync with project_id.
    """
    from longhand.analysis.project_inference import find_project_root_strict
    from longhand.parser import _resolve_cwd_to_project

    try:
        with store.sqlite.connect() as conn:
            rows = conn.execute(
                "SELECT cwd, COUNT(*) AS n FROM events "
                "WHERE session_id = ? AND cwd IS NOT NULL AND cwd != '' "
                "GROUP BY cwd ORDER BY n DESC",
                (session_id,),
            ).fetchall()
    except Exception:
        return None

    try:
        home_resolved = Path.home().resolve()
    except (OSError, PermissionError):
        home_resolved = None

    counts: dict[str, int] = {}
    for row in rows[:20]:  # bounded, mirrors _MAX_UNIQUE_CWDS_SCANNED
        root = _resolve_cwd_to_project(row["cwd"], home_resolved, find_project_root_strict)
        if root:
            counts[root] = counts.get(root, 0) + int(row["n"])
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def ingest_live_tail(
    transcript: str,
    data_dir: str | None = None,
) -> dict:
    """Tail-read new bytes of a Claude Code transcript and upsert their events.

    Called by the Stop hook (fires once per assistant turn). Idempotent and
    fast — skips heavy analysis (episodes, segments, embeddings, project
    inference). The full pass still runs at SessionEnd; this just keeps the
    events table fresh during the session so a crash doesn't lose work.

    Returns a small dict with counts. Never raises — designed to be called
    from a hook chain that must not crash Claude Code.
    """
    from longhand.recall.project_fallback import (
        claim_ingest_lock,
        release_ingest_lock,
    )

    summary: dict = {
        "events": 0,
        "session_id": None,
        "skipped": None,
        "advanced_to": None,
    }

    path = Path(transcript).expanduser()
    if not path.exists():
        summary["skipped"] = "transcript-missing"
        return summary

    try:
        current_size = path.stat().st_size
    except OSError:
        summary["skipped"] = "stat-failed"
        return summary

    if current_size == 0:
        summary["skipped"] = "empty"
        return summary

    locked = False
    try:
        # LonghandStore() runs migrations and opens ChromaDB — it can raise
        # (corrupted store, disk full, migration race), so it must sit inside
        # this guard: nothing in this function may raise into the Stop hook.
        store = LonghandStore(data_dir=data_dir)

        if store.sqlite.live_caught_up(str(path), current_size):
            summary["skipped"] = "caught-up"
            return summary

        # Non-blocking lock — if a heavier ingest is running, defer to next Stop.
        if not claim_ingest_lock(store):
            summary["skipped"] = "locked"
            return summary
        locked = True

        start_offset = store.sqlite.get_live_offset(str(path))

        try:
            parser = JSONLParser(path)
        except (ValueError, FileNotFoundError):
            summary["skipped"] = "parser-init-failed"
            return summary

        new_events, safe_offset = parser.parse_tail_from_offset(start_offset, base_sequence=0)

        if safe_offset <= start_offset:
            # No complete line yet — leave offset unchanged.
            summary["skipped"] = "no-complete-line"
            return summary

        if not new_events:
            # Complete lines but they didn't parse into events (e.g. queue
            # operations, file-history snapshots that yielded nothing). Still
            # advance the offset so we don't re-read them.
            with store.sqlite.connect() as conn:
                row = conn.execute(
                    "SELECT session_id FROM ingestion_log WHERE transcript_path = ?",
                    (str(path),),
                ).fetchone()
            existing = row["session_id"] if row else None
            if existing:
                store.sqlite.update_live_progress(
                    transcript_path=str(path),
                    session_id=existing,
                    last_offset=safe_offset,
                    event_count=0,
                )
                summary["session_id"] = existing
                summary["advanced_to"] = safe_offset
            else:
                summary["skipped"] = "no-session-id"
            return summary

        session_id = next((e.session_id for e in new_events if e.session_id), None)
        if not session_id:
            with store.sqlite.connect() as conn:
                row = conn.execute(
                    "SELECT session_id FROM ingestion_log WHERE transcript_path = ?",
                    (str(path),),
                ).fetchone()
            session_id = row["session_id"] if row else None
        if not session_id:
            summary["skipped"] = "no-session-id"
            return summary

        # Re-derive sequence numbers so the new tail slots after existing rows.
        with store.sqlite.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) AS s FROM events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            base_seq = int(row["s"]) + 1 if row else 0

        for idx, event in enumerate(new_events):
            event.sequence = base_seq + idx
            if not event.session_id:
                event.session_id = session_id

        store.sqlite.insert_events(new_events)
        pairs = store.sqlite.build_tool_pairs_from_events(new_events)
        if pairs:
            store.sqlite.upsert_tool_pairs(pairs)

        # Aggregate session stats from the events table so the in-progress
        # session row is queryable via list_sessions / search.
        with store.sqlite.connect() as conn:
            agg = conn.execute(
                """
                SELECT
                    COUNT(*) AS event_count,
                    MIN(timestamp) AS started_at,
                    MAX(timestamp) AS ended_at,
                    SUM(CASE WHEN event_type = 'user_message' THEN 1 ELSE 0 END) AS user_count,
                    SUM(CASE WHEN event_type LIKE 'assistant_%' THEN 1 ELSE 0 END) AS asst_count,
                    SUM(CASE WHEN event_type = 'tool_call' THEN 1 ELSE 0 END) AS tool_count,
                    SUM(
                        CASE WHEN event_type = 'tool_call'
                             AND file_operation IN ('edit','write','multi_edit','notebook_edit')
                        THEN 1 ELSE 0 END
                    ) AS edit_count,
                    MAX(git_branch) AS git_branch,
                    MAX(model) AS model
                FROM events
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            existing_session = conn.execute(
                "SELECT started_at, project_path FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        started_at = (agg["started_at"] if agg and agg["started_at"] else None) or (
            existing_session["started_at"] if existing_session else utcnow().isoformat()
        )
        ended_at = agg["ended_at"] if agg and agg["ended_at"] else utcnow().isoformat()

        # Derive cwd the same way build_session does (mode of project-resolving
        # cwds), not MAX(cwd). None → the UPSERT's COALESCE preserves the existing
        # cwd, so an in-progress session never drifts out of sync with its project.
        best_cwd = _live_best_project_cwd(store, session_id)

        with store.sqlite.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, project_path, transcript_path, started_at, ended_at,
                    event_count, user_message_count, assistant_message_count,
                    tool_call_count, file_edit_count, git_branch, cwd, model, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    transcript_path = excluded.transcript_path,
                    ended_at = excluded.ended_at,
                    event_count = excluded.event_count,
                    user_message_count = excluded.user_message_count,
                    assistant_message_count = excluded.assistant_message_count,
                    tool_call_count = excluded.tool_call_count,
                    file_edit_count = excluded.file_edit_count,
                    cwd = COALESCE(excluded.cwd, sessions.cwd),
                    git_branch = COALESCE(excluded.git_branch, sessions.git_branch),
                    model = COALESCE(excluded.model, sessions.model),
                    ingested_at = excluded.ingested_at
                """,
                (
                    session_id,
                    existing_session["project_path"] if existing_session else best_cwd,
                    str(path),
                    started_at,
                    ended_at,
                    int(agg["event_count"]) if agg else len(new_events),
                    int(agg["user_count"] or 0) if agg else 0,
                    int(agg["asst_count"] or 0) if agg else 0,
                    int(agg["tool_count"] or 0) if agg else 0,
                    int(agg["edit_count"] or 0) if agg else 0,
                    agg["git_branch"] if agg else None,
                    best_cwd,
                    agg["model"] if agg else None,
                    utcnow().isoformat(),
                ),
            )

        store.sqlite.update_live_progress(
            transcript_path=str(path),
            session_id=session_id,
            last_offset=safe_offset,
            event_count=int(agg["event_count"]) if agg else len(new_events),
        )

        summary["events"] = len(new_events)
        summary["session_id"] = session_id
        summary["advanced_to"] = safe_offset
        return summary

    except Exception as e:
        summary["skipped"] = f"error:{type(e).__name__}"
        return summary
    finally:
        if locked:
            release_ingest_lock(store)


# ─── Doctor ────────────────────────────────────────────────────────────────


def _freshness_status(store: LonghandStore) -> str | None:
    """Check that recent on-disk JSONLs have corresponding sessions rows.

    Returns a Rich-formatted string, or None if the check couldn't be performed
    (e.g. ~/.claude/projects doesn't exist yet). For each JSONL whose mtime is
    within the last 7 days, check whether the sessions table has a row for it.
    Ratio drives the status: ≥0.9 green, ≥0.5 yellow, <0.5 red.
    """
    import time as _time

    try:
        jsonls = discover_sessions()
    except Exception:
        return None
    if not jsonls:
        return None

    cutoff = _time.time() - 7 * 86400
    recent: list[str] = []
    for j in jsonls:
        try:
            if j.stat().st_mtime >= cutoff:
                recent.append(str(j))
        except (OSError, PermissionError):
            continue

    if not recent:
        return "[green]✓[/green] no recent Claude Code activity"

    try:
        with store.sqlite.connect() as conn:
            placeholders = ",".join("?" * len(recent))
            row = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE transcript_path IN ({placeholders})",
                recent,
            ).fetchone()
            ingested = row[0] if row else 0
    except Exception:
        return None

    ratio = ingested / len(recent)
    summary = f"{ingested}/{len(recent)} transcripts ingested"

    if ratio >= 0.9:
        return f"[green]✓[/green] {summary}"
    if ratio >= 0.5:
        return (
            f"[yellow]⚠[/yellow] {summary} — run [bold]longhand reconcile --fix[/bold] to catch up"
        )
    return (
        f"[red]✗[/red] {summary} — hook may be silently failing. "
        "Run [bold]longhand reconcile --fix[/bold] and verify "
        "[bold]longhand hook install[/bold]."
    )


def _transcript_format_status(store: LonghandStore, days: int = 30) -> str:
    """Rich-formatted upstream-drift check: unknown transcript entry types.

    Claude Code adds entry types without notice; the parser preserves them as
    event_type='unknown' rows with raw_json intact. A pile of one NEW type
    means the transcript format drifted — a newer longhand probably reads it.
    Types already dispositioned (KNOWN_SKIP_ENTRY_TYPES members stored before
    the skip existed, or deliberately-preserved TRIAGED_UNKNOWN_ENTRY_TYPES)
    are excluded — alarming on understood types forever would train users to
    ignore the row. The fixture corpus in tests/fixtures/transcript_shapes/
    regression-gates every known type; this row is how new drift surfaces on
    a live store.
    """
    from collections import Counter

    from longhand.parser import KNOWN_SKIP_ENTRY_TYPES, TRIAGED_UNKNOWN_ENTRY_TYPES

    cutoff = (utcnow() - timedelta(days=days)).isoformat()
    try:
        with store.sqlite.connect() as conn:
            rows = conn.execute(
                "SELECT raw_json FROM events "
                "WHERE event_type = 'unknown' AND timestamp >= ? LIMIT 500",
                (cutoff,),
            ).fetchall()
    except Exception:
        return "[dim]—[/dim] could not inspect events"

    # Counter over the raw type in Python — JSON1 availability varies across
    # bundled SQLites, so no json_extract in the query.
    dispositioned = KNOWN_SKIP_ENTRY_TYPES | TRIAGED_UNKNOWN_ENTRY_TYPES
    counter: Counter[str] = Counter()
    for row in rows:
        try:
            entry_type = str(json.loads(row["raw_json"]).get("type", "?"))
        except Exception:
            entry_type = "?"
        if entry_type not in dispositioned:
            counter[entry_type] += 1

    if not counter:
        return f"[green]✓[/green] no undispositioned entry types in the last {days} days"

    top = ", ".join(f"{name} ×{n}" for name, n in counter.most_common(3))
    capped = "+" if len(rows) == 500 else ""
    return (
        f"[yellow]⚠[/yellow] {sum(counter.values())}{capped} unrecognized entries in the "
        f"last {days} days ({top}) — the transcript format may have drifted; "
        "try [bold]pip install -U longhand[/bold]"
    )


# Hook-error classes that `reconcile` can actually heal. reconcile enumerates
# from DISK (discover_sessions), so it can only re-ingest a transcript that is
# still there: `ingest-failed` qualifies, and nothing else does. A
# `missing-transcript` never landed and is invisible to the heal path forever;
# `oversize-transcript` would hit the same cap on re-ingest; `store-open-failed`
# blocks reconcile itself. Recommending reconcile for those is a no-op, which
# is a Promise 5 (honest metrics) violation — over the v0.13 bake, 21 of 23
# real hook errors were the unhealable class.
_RECONCILE_HEALABLE = frozenset({"ingest-failed"})


def _hook_error_class(line: str) -> str:
    """Extract the class token from a breadcrumb line, or "unknown".

    _log_hook_error writes ``<iso-ts> <command> <class>: <detail>`` — the
    class token is already there, so the split needs no new plumbing.
    """
    parts = line.split()
    if len(parts) < 3 or not parts[2].endswith(":"):
        return "unknown"
    return parts[2].removesuffix(":")


def _hook_errors_status(store: LonghandStore, days: int = 7) -> str:
    """Rich-formatted count of hook-error breadcrumbs in the last `days` days.

    Complements _freshness_status: freshness says "something is missing";
    this row says why — every hook-mode ingest failure leaves one line in
    logs/hook-errors-YYYY-MM-DD.log (see _log_hook_error).

    The remedy is split by class: only classes reconcile can genuinely heal
    get the reconcile advice. Anything else says plainly that there is
    nothing to heal, rather than recommending a command that cannot help.
    """
    from collections import Counter

    logs = store.data_dir / "logs"
    window_start = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    counts: Counter[str] = Counter()
    for f in logs.glob("hook-errors-*.log"):
        try:
            day = date.fromisoformat(f.stem.removeprefix("hook-errors-"))
        except ValueError:
            continue  # unrecognized filename — not ours to count
        if day < window_start:
            continue
        try:
            with f.open() as fh:
                counts.update(_hook_error_class(line) for line in fh if line.strip())
        except OSError:
            continue
    count = sum(counts.values())
    if count == 0:
        return f"[green]✓[/green] none in the last {days} days"

    healable = sum(n for cls, n in counts.items() if cls in _RECONCILE_HEALABLE)
    # Largest class first so the dominant failure mode leads the remedy.
    breakdown = ", ".join(
        f"{n} {cls}" for cls, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    if healable == count:
        remedy = "[bold]longhand reconcile --fix[/bold] heals missed ingests"
    elif healable:
        remedy = (
            f"[bold]longhand reconcile --fix[/bold] heals the {healable} ingest-failed; "
            "the rest were never written — nothing to heal"
        )
    else:
        remedy = "these were never written — nothing to heal"
    return (
        f"[yellow]⚠[/yellow] {count} in the last {days} days ({breakdown}) — see "
        f"[bold]{logs}/hook-errors-*.log[/bold]; {remedy}"
    )


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def doctor(json_out: bool = False) -> None:
    """Diagnose Longhand installation and data."""
    rows: list[tuple[str, str]] = []

    def _row(label: str, value: str) -> None:
        rows.append((label, value))

    # 1. longhand on PATH?
    longhand_bin = shutil.which("longhand")
    if longhand_bin:
        _row("longhand CLI", f"[green]✓[/green] {longhand_bin}")
    else:
        _row(
            "longhand CLI",
            "[yellow]⚠[/yellow] not on PATH (run [bold]pip install longhand[/bold])",
        )

    # 1b. Version freshness — the ONLY doctor check that goes online (a single
    # HTTPS GET to pypi.org; opt-out via LONGHAND_NO_UPDATE_CHECK=1).
    from longhand import update_check

    _row("Version", update_check.doctor_status())

    # 2. SessionEnd + Stop + UserPromptSubmit hooks installed?
    hook_installed = False
    hook_stale = False
    stop_hook_installed = False
    prompt_hook_installed = False
    if CLAUDE_SETTINGS_PATH.exists():
        settings = _load_json(CLAUDE_SETTINGS_PATH)
        for h in settings.get("hooks", {}).get("SessionEnd", []):
            if _entry_contains_command(h, "longhand ingest-session"):
                hook_installed = True
                if _hook_command_is_stale(h):
                    hook_stale = True
                break
        for h in settings.get("hooks", {}).get("Stop", []):
            if _entry_contains_command(h, "longhand ingest-live"):
                stop_hook_installed = True
                break
        for h in settings.get("hooks", {}).get("UserPromptSubmit", []):
            if _entry_contains_command(h, "__prompt-hook-run"):
                prompt_hook_installed = True
                break

    if hook_stale:
        _row(
            "SessionEnd hook",
            "[red]✗[/red] stale — uses pre-0.5.2 $CLAUDE_TRANSCRIPT_PATH "
            "(silently failing). Run [bold]longhand hook install[/bold] to upgrade.",
        )
    elif hook_installed:
        _row("SessionEnd hook", "[green]✓[/green] installed (final/full ingest)")
    else:
        _row(
            "SessionEnd hook",
            "[yellow]⚠[/yellow] not installed (run [bold]longhand hook install[/bold])",
        )

    if stop_hook_installed:
        _row("Stop hook", "[green]✓[/green] installed (live tail per turn)")
    else:
        _row(
            "Stop hook",
            "[yellow]⚠[/yellow] not installed — in-progress sessions invisible. "
            "Run [bold]longhand hook install[/bold].",
        )

    if prompt_hook_installed:
        _row("UserPromptSubmit hook", "[green]✓[/green] installed (auto-context)")
    else:
        _row(
            "UserPromptSubmit hook",
            "[yellow]⚠[/yellow] not installed (run [bold]longhand prompt-hook install[/bold])",
        )

    # 2b. Reconciler launchd job (macOS only)
    if sys.platform == "darwin":
        if RECONCILER_PLIST_PATH.exists():
            _row(
                "Reconciler job",
                "[green]✓[/green] installed (launchd, every 30 min)",
            )
        else:
            _row(
                "Reconciler job",
                "[dim]—[/dim] not installed "
                "(run [bold]longhand schedule install-reconciler[/bold])",
            )

    # 3. Claude Desktop MCP installed?
    mcp_installed = False
    if CLAUDE_DESKTOP_CONFIG_PATH.exists():
        config = _load_json(CLAUDE_DESKTOP_CONFIG_PATH)
        if "longhand" in config.get("mcpServers", {}):
            mcp_installed = True

    if mcp_installed:
        _row("Claude Desktop MCP", "[green]✓[/green] installed")
    else:
        _row(
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
        _row("Claude Code MCP", "[green]✓[/green] installed")
    else:
        _row(
            "Claude Code MCP",
            "[dim]—[/dim] not installed (run [bold]claude mcp add longhand -s user -- longhand mcp-server[/bold])",
        )

    # 4. Data directory
    store = LonghandStore()
    data_ok = store.data_dir.exists() and store.data_dir.is_dir()
    from longhand.storage.store import data_dir_source

    _row(
        "Data directory",
        (f"[green]✓[/green] {store.data_dir}" if data_ok else f"[red]✗[/red] {store.data_dir}")
        + f" [dim](source: {data_dir_source()})[/dim]",
    )

    # 5. Recent ingest freshness — detects silently broken hooks. If Claude
    # Code wrote N transcripts in the last 7 days but only M got ingested,
    # the hook is either misconfigured or failing quietly.
    freshness_row = _freshness_status(store)
    if freshness_row:
        _row("Recent ingest (7d)", freshness_row)

    # 5b. Hook failures breadcrumbed by ingest-session's hook mode — freshness
    # says "something is missing"; this row says why.
    _row("Hook errors (7d)", _hook_errors_status(store))

    # 5c. Upstream-drift canary: unknown transcript entry types accumulating
    # means Claude Code's format moved and this longhand can't read the new
    # entries yet.
    _row("Transcript format", _transcript_format_status(store))

    # 6. Stats
    stats = store.stats()
    _row("Sessions ingested", f"{stats.get('sessions', 0):,}")
    _row("Events stored", f"{stats.get('events', 0):,}")
    _row("Projects inferred", f"{stats.get('projects', 0):,}")
    _row("Episodes extracted", f"{stats.get('episodes', 0):,}")

    sessions_needing_analysis = max(0, stats.get("sessions", 0) - stats.get("outcomes", 0))
    if sessions_needing_analysis > 0:
        _row(
            "Sessions needing analysis",
            f"[yellow]{sessions_needing_analysis}[/yellow] (run [bold]longhand analyze --all[/bold])",
        )

    # 7. Storage footprint — the corpus grows forever by design, but nothing
    # surfaced how big it had gotten (3 GB observed in the wild).
    db_file = store.data_dir / "longhand.db"
    db_size = db_file.stat().st_size if db_file.exists() else 0
    chroma_dir = store.data_dir / "chroma"
    chroma_size = (
        sum(f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file())
        if chroma_dir.exists()
        else 0
    )
    _row("Storage", f"SQLite {_human_size(db_size)} · vectors {_human_size(chroma_size)}")

    with store.sqlite.connect() as conn:
        aux_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'unknown'"
        ).fetchone()[0]
    if aux_events > 0:
        _row(
            "Aux/unknown events",
            f"[yellow]{aux_events:,}[/yellow] stored orchestration noise "
            "(reclaim: [bold]longhand db vacuum --prune-aux[/bold])",
        )

    if json_out:
        from rich.text import Text as _Text

        print(
            json.dumps({label: _Text.from_markup(value).plain for label, value in rows}, indent=2)
        )
        return

    table = Table(title="Longhand Doctor", show_header=False, border_style="cyan")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)
