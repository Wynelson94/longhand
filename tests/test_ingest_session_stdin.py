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

import pytest
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

    fresh = {"hooks": [{"type": "command", "command": "/usr/local/bin/longhand ingest-session"}]}
    assert _hook_command_is_stale(fresh) is False

    unrelated = {"hooks": [{"type": "command", "command": "echo hello"}]}
    assert _hook_command_is_stale(unrelated) is False


# ─── hook mode never exits nonzero (v0.13 hardening) ─────────────────────────
#
# stdin-invoked = hook mode: every failure becomes a one-line stderr note plus
# a breadcrumb in logs/hook-errors-YYYY-MM-DD.log (surfaced by doctor), and the
# command exits 0 so it can never crash the SessionEnd hook chain. Explicit
# --transcript keeps loud exit-1 failures — human misuse deserves an error.


def _stdin_payload(path: Path) -> str:
    return json.dumps({"transcript_path": str(path), "session_id": "s-hook"})


def _hook_error_lines(data_dir: Path) -> list[str]:
    logs = data_dir / "logs"
    lines: list[str] = []
    for f in sorted(logs.glob("hook-errors-*.log")):
        lines.extend(f.read_text().splitlines())
    return lines


def test_hook_mode_missing_transcript_exits_zero_and_logs(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / "lh"

    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=_stdin_payload(tmp_path / "gone.jsonl"),
    )

    assert result.exit_code == 0, result.output
    lines = _hook_error_lines(data_dir)
    assert len(lines) == 1
    assert "missing-transcript" in lines[0]
    assert "gone.jsonl" in lines[0]


def test_hook_mode_too_new_db_exits_zero_and_logs(tmp_path: Path) -> None:
    """Store-open failures (incl. SchemaTooNewError from the downgrade guard)
    must never crash the hook — the breadcrumb carries the upgrade advice."""
    from longhand.storage.migrations import MAX_KNOWN_MIGRATION
    from longhand.storage.sqlite_store import SQLiteStore

    data_dir = tmp_path / "lh"
    seeded = SQLiteStore(data_dir / "longhand.db")
    with seeded.connect() as conn:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (MAX_KNOWN_MIGRATION + 1, "2027-01-01T00:00:00Z"),
        )
        conn.commit()

    transcript = tmp_path / "some.jsonl"
    transcript.write_text(json.dumps({"type": "user", "uuid": "u1"}) + "\n")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=_stdin_payload(transcript),
    )

    assert result.exit_code == 0, result.output
    lines = _hook_error_lines(data_dir)
    assert len(lines) == 1
    assert "store-open-failed" in lines[0]
    assert "SchemaTooNewError" in lines[0]


def test_hook_mode_oversize_transcript_exits_zero_and_logs(
    sample_session_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parser's size cap becomes a distinct breadcrumb, not a crash."""
    import longhand.parser as parser_mod

    monkeypatch.setattr(parser_mod, "MAX_FILE_SIZE_BYTES", 1)

    runner = CliRunner()
    data_dir = tmp_path / "lh"
    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=_stdin_payload(sample_session_file),
    )

    assert result.exit_code == 0, result.output
    lines = _hook_error_lines(data_dir)
    assert len(lines) == 1
    assert "oversize-transcript" in lines[0]


def test_hook_mode_ingest_failure_exits_zero_logs_and_releases_lock(
    sample_session_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exploding pipeline stage becomes a breadcrumb; reconcile heals later."""
    from longhand.storage import LonghandStore

    def _boom(self, *args, **kwargs):
        raise RuntimeError("pipeline exploded mid-ingest")

    monkeypatch.setattr(LonghandStore, "ingest_session", _boom)

    runner = CliRunner()
    data_dir = tmp_path / "lh"
    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=_stdin_payload(sample_session_file),
    )

    assert result.exit_code == 0, result.output
    lines = _hook_error_lines(data_dir)
    assert len(lines) == 1
    assert "ingest-failed" in lines[0]
    assert "RuntimeError" in lines[0]
    assert not (data_dir / ".ingest.lock").exists()  # released in the finally


def test_explicit_transcript_missing_still_exits_one(tmp_path: Path) -> None:
    """Human misuse keeps its loud exit — only hook mode is silenced."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "ingest-session",
            "--transcript",
            str(tmp_path / "gone.jsonl"),
            "--data-dir",
            str(tmp_path / "lh"),
        ],
    )

    assert result.exit_code == 1
    assert not (tmp_path / "lh" / "logs").exists()  # breadcrumbs are hook-mode only


def test_hook_error_logging_failure_is_swallowed(tmp_path: Path) -> None:
    """Even when the breadcrumb log cannot be written, hook mode exits 0."""
    data_dir = tmp_path / "lh"
    data_dir.mkdir(parents=True)
    (data_dir / "logs").write_text("i am a file, not a directory")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=_stdin_payload(tmp_path / "gone.jsonl"),
    )

    assert result.exit_code == 0, result.output
