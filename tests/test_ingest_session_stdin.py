"""Regression tests for the SessionEnd-hook stdin contract (v0.5.2+).

Modern Claude Code passes hook data as JSON on stdin rather than via the
`$CLAUDE_TRANSCRIPT_PATH` env var that older Claude Code versions used. The
`longhand ingest-session` command must therefore read `transcript_path` from
stdin when `--transcript` is not supplied.

These tests pin that contract so we never silently break hook ingest again.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from longhand.cli import app
from longhand.setup_commands import _hook_command_is_stale


def test_ingest_session_reads_transcript_from_stdin(
    sample_session_file: Path, tmp_path: Path
) -> None:
    """--transcript omitted → command reads transcript_path from stdin JSON."""
    runner = CliRunner()
    data_dir = tmp_path / "longhand-data"

    payload = json.dumps(
        {"transcript_path": str(sample_session_file), "session_id": "test-session-1"}
    )

    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=payload,
    )

    assert result.exit_code == 0, result.output
    assert "Ingested" in result.stdout
    # The SQLite DB should now exist with the session recorded.
    assert (data_dir / "longhand.db").exists()


def test_ingest_session_exits_silently_without_transcript(tmp_path: Path) -> None:
    """No --transcript AND empty stdin → silent no-op (must not crash hook chain)."""
    runner = CliRunner()
    data_dir = tmp_path / "longhand-data"

    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input="",
    )

    # Exit 0 is critical — SessionEnd hooks must never error out just because
    # they were invoked without the expected payload.
    assert result.exit_code == 0


def test_ingest_session_survives_malformed_stdin(tmp_path: Path) -> None:
    """Garbage stdin → silent no-op, no crash."""
    runner = CliRunner()
    data_dir = tmp_path / "longhand-data"

    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input="this is not json at all {{{",
    )

    assert result.exit_code == 0


def test_hook_command_is_stale_detects_env_var_version() -> None:
    """The stale-hook detector must recognize the pre-0.5.2 command format."""
    stale = {
        "hooks": [
            {
                "type": "command",
                "command": '/usr/local/bin/longhand ingest-session --transcript "$CLAUDE_TRANSCRIPT_PATH"',
            }
        ]
    }
    assert _hook_command_is_stale(stale) is True

    fresh = {
        "hooks": [
            {"type": "command", "command": "/usr/local/bin/longhand ingest-session"}
        ]
    }
    assert _hook_command_is_stale(fresh) is False

    unrelated = {"hooks": [{"type": "command", "command": "echo hello"}]}
    assert _hook_command_is_stale(unrelated) is False
