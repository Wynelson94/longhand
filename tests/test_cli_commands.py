"""Smoke tests for the CLI entry points.

These exercise Typer's CliRunner against the live `longhand` app, isolating
side effects (settings.json writes, data dirs) under tmp_path. Coverage is
focused on the high-risk install/setup commands and the read-only commands
that show up most often in doctor/debug flows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME so ~/.claude/ writes land in tmp_path.

    setup_commands resolves CLAUDE_SETTINGS_PATH at import time, so HOME
    monkeypatching alone isn't enough — patch the resolved module constants
    directly as well.
    """
    from longhand import setup_commands

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LONGHAND_HOME", str(tmp_path / ".longhand"))
    monkeypatch.setattr(
        setup_commands, "CLAUDE_SETTINGS_PATH", tmp_path / ".claude" / "settings.json"
    )
    return tmp_path


# ─── Surface sanity ─────────────────────────────────────────────────────────


def test_cli_help_renders(runner: CliRunner):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Persistent local memory" in result.stdout


def test_cli_stats_help(runner: CliRunner):
    result = runner.invoke(app, ["stats", "--help"])
    assert result.exit_code == 0


def test_cli_search_help(runner: CliRunner):
    result = runner.invoke(app, ["search", "--help"])
    assert result.exit_code == 0
    assert "Semantic" in result.stdout or "search" in result.stdout.lower()


def test_cli_recall_help(runner: CliRunner):
    result = runner.invoke(app, ["recall", "--help"])
    assert result.exit_code == 0


def test_cli_unknown_command(runner: CliRunner):
    result = runner.invoke(app, ["not-a-real-command"])
    assert result.exit_code != 0


# ─── Hook / install commands ────────────────────────────────────────────────


def test_cli_hook_help(runner: CliRunner):
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    assert "install" in result.stdout.lower()


def test_cli_prompt_hook_help(runner: CliRunner):
    result = runner.invoke(app, ["prompt-hook", "--help"])
    assert result.exit_code == 0


def test_cli_mcp_help(runner: CliRunner):
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0


def test_cli_hook_install_writes_settings(runner: CliRunner, isolated_home: Path):
    """hook install must create ~/.claude/settings.json with a SessionEnd entry."""
    # Pre-seed an empty ~/.claude dir so the command has somewhere to write
    (isolated_home / ".claude").mkdir()

    result = runner.invoke(app, ["hook", "install"])
    assert result.exit_code == 0, f"hook install failed: {result.stdout}"

    settings_path = isolated_home / ".claude" / "settings.json"
    assert settings_path.exists(), "hook install should create settings.json"

    settings = json.loads(settings_path.read_text())
    hooks = settings.get("hooks", {})
    assert "SessionEnd" in hooks, "SessionEnd hook should be registered"


def test_cli_hook_install_idempotent(runner: CliRunner, isolated_home: Path):
    """Running hook install twice must not duplicate entries."""
    (isolated_home / ".claude").mkdir()

    runner.invoke(app, ["hook", "install"])
    runner.invoke(app, ["hook", "install"])

    settings = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    session_end = settings.get("hooks", {}).get("SessionEnd", [])
    # Flatten all "longhand ingest-session" commands
    longhand_entries = [
        entry
        for group in session_end
        for entry in group.get("hooks", [])
        if "longhand" in (entry.get("command") or "").lower()
    ]
    assert len(longhand_entries) == 1, (
        f"expected 1 longhand hook entry, got {len(longhand_entries)}"
    )


def test_cli_hook_install_then_uninstall(runner: CliRunner, isolated_home: Path):
    """Uninstall must remove the hook added by install."""
    (isolated_home / ".claude").mkdir()

    runner.invoke(app, ["hook", "install"])
    settings = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    assert "SessionEnd" in settings.get("hooks", {})

    result = runner.invoke(app, ["hook", "uninstall"])
    assert result.exit_code == 0

    settings_after = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    session_end = settings_after.get("hooks", {}).get("SessionEnd", [])
    remaining_longhand = [
        entry
        for group in session_end
        for entry in group.get("hooks", [])
        if "longhand" in (entry.get("command") or "").lower()
    ]
    assert remaining_longhand == [], "uninstall should remove longhand hook entries"


def test_cli_prompt_hook_install(runner: CliRunner, isolated_home: Path):
    """prompt-hook install must register a UserPromptSubmit hook."""
    (isolated_home / ".claude").mkdir()

    result = runner.invoke(app, ["prompt-hook", "install"])
    assert result.exit_code == 0, f"prompt-hook install failed: {result.stdout}"

    settings = json.loads((isolated_home / ".claude" / "settings.json").read_text())
    assert "UserPromptSubmit" in settings.get("hooks", {})
