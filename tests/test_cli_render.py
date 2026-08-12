"""Behavioral tests for the read/render CLI commands.

cli/_commands.py is the primary user surface and was ~15% covered — the render
commands (stats, sessions, timeline, history, replay, export, config, doctor)
had no behavioral tests, which is how a broken command could ship unnoticed.
These drive each against a seeded store via CliRunner.

Rich output is made deterministic for substring assertions by widening the
terminal and stripping ANSI (the suite otherwise flakes when FORCE_COLOR is set
in the environment — see the reconcile tests).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand.cli import app

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(s: str) -> str:
    """Strip ANSI escapes so assertions don't depend on color settings."""
    return _ANSI.sub("", s)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _deterministic_rich(monkeypatch: pytest.MonkeyPatch):
    # Wide + no forced color → Rich renders stable, unwrapped, ANSI-free text
    # regardless of the caller's terminal/FORCE_COLOR.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture
def seeded(tmp_path: Path, sample_session_file: Path):
    """A temp store with the sample session ingested. Returns (data_dir, session_id)."""
    from longhand.parser import JSONLParser
    from longhand.storage import LonghandStore

    data_dir = tmp_path / "longhand"
    store = LonghandStore(data_dir=data_dir)
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)

    sid = store.sqlite.list_sessions(limit=10)[0]["session_id"]
    return data_dir, sid


# ─── stats ──────────────────────────────────────────────────────────────────


def test_stats_renders_counts(runner: CliRunner, seeded):
    data_dir, _ = seeded
    result = runner.invoke(app, ["stats", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    out = plain(result.stdout)
    for label in ("Sessions", "Events", "Tool calls", "Data directory"):
        assert label in out, f"stats output missing {label!r}"


# ─── sessions ─────────────────────────────────────────────────────────────────


def test_sessions_lists_ingested(runner: CliRunner, seeded):
    data_dir, sid = seeded
    result = runner.invoke(app, ["sessions", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    out = plain(result.stdout)
    assert "Longhand Sessions" in out
    assert sid[:8] in out


def test_sessions_empty_store_is_friendly(runner: CliRunner, tmp_path: Path):
    result = runner.invoke(app, ["sessions", "--data-dir", str(tmp_path / "empty")])
    assert result.exit_code == 0, result.stdout
    assert "No sessions found" in plain(result.stdout)


# ─── timeline ─────────────────────────────────────────────────────────────────


def test_timeline_renders_session_events(runner: CliRunner, seeded):
    data_dir, sid = seeded
    result = runner.invoke(app, ["timeline", sid[:8], "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    out = plain(result.stdout)
    # The sample session's first user message.
    assert "Edit the readme" in out


def test_timeline_unknown_session_exits_nonzero(runner: CliRunner, seeded):
    data_dir, _ = seeded
    result = runner.invoke(app, ["timeline", "zzzznope", "--data-dir", str(data_dir)])
    assert result.exit_code == 1
    assert "No session found" in plain(result.stdout)


# ─── history ──────────────────────────────────────────────────────────────────


def test_history_lists_file_edits(runner: CliRunner, seeded):
    data_dir, _ = seeded
    result = runner.invoke(app, ["history", "README.md", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    out = plain(result.stdout)
    assert "File History" in out
    assert "README.md" in out


def test_history_no_edits_is_friendly(runner: CliRunner, seeded):
    data_dir, _ = seeded
    result = runner.invoke(app, ["history", "does-not-exist.xyz", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    assert "No edits found" in plain(result.stdout)


# ─── replay ───────────────────────────────────────────────────────────────────


def test_replay_reconstructs_written_file(runner: CliRunner, seeded):
    data_dir, sid = seeded
    # new.txt was created with a Write of "Hello, World!".
    result = runner.invoke(
        app,
        ["replay", sid[:8], "/tmp/test-project/new.txt", "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.stdout
    out = plain(result.stdout)
    assert "Reconstructed State" in out
    assert "Hello" in out


# ─── export ───────────────────────────────────────────────────────────────────


def test_export_session_writes_markdown_file(runner: CliRunner, seeded, tmp_path: Path):
    data_dir, sid = seeded
    out_file = tmp_path / "session.md"
    result = runner.invoke(
        app,
        ["export", sid[:8], "--out", str(out_file), "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, result.stdout
    assert "Exported to" in plain(result.stdout)
    assert out_file.exists()
    assert out_file.read_text().strip(), "exported markdown should not be empty"


# ─── config (HOME-scoped, no data_dir) ───────────────────────────────────────


def test_config_set_then_show(runner: CliRunner, tmp_path: Path, monkeypatch):
    # config reads/writes ~/.longhand/config.json — isolate HOME.
    monkeypatch.setenv("HOME", str(tmp_path))

    set_result = runner.invoke(app, ["config", "--set", "hook.min_relevance=3.5"])
    assert set_result.exit_code == 0, set_result.stdout
    assert "Set hook.min_relevance" in plain(set_result.stdout)

    cfg = json.loads((tmp_path / ".longhand" / "config.json").read_text())
    assert cfg["hook"]["min_relevance"] == 3.5

    show_result = runner.invoke(app, ["config"])
    assert show_result.exit_code == 0, show_result.stdout
    assert "hook.min_relevance" in plain(show_result.stdout)


def test_config_rejects_unknown_namespace(runner: CliRunner, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["config", "--set", "bogus.key=1"])
    assert result.exit_code == 0
    assert "Only hook.* and redact.* keys" in plain(result.stdout)


def test_config_accepts_redact_namespace(runner: CliRunner, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["config", "--set", "redact.enabled=true"])
    assert result.exit_code == 0
    assert "Set redact.enabled = True" in plain(result.stdout)

    import json as _json

    saved = _json.loads((tmp_path / ".longhand" / "config.json").read_text())
    assert saved["redact"]["enabled"] is True


# ─── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_runs_clean(runner: CliRunner, tmp_path: Path, monkeypatch):
    # doctor uses the default (HOME-based) store; isolate HOME so it inspects an
    # empty environment rather than the developer's real ~/.longhand / ~/.claude.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LONGHAND_HOME", str(tmp_path / ".longhand"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stdout
    assert plain(result.stdout).strip(), "doctor should print a report"


# ─── v0.11 surface trim ───────────────────────────────────────────────────────


def test_help_shows_grouped_panels(runner: CliRunner):
    out = plain(runner.invoke(app, ["--help"]).stdout)
    for panel in (
        "Recall",
        "Archaeology",
        "Browse & insights",
        "Data",
        "Setup & health",
        "Plumbing",
    ):
        assert panel in out, f"missing help panel: {panel}"


def test_help_hides_plumbing_commands(runner: CliRunner):
    out = plain(runner.invoke(app, ["--help"]).stdout)
    for hidden_cmd in (
        "ingest-session",
        "ingest-live",
        "backfill-episodes",
        "mcp-server",
    ):
        assert hidden_cmd not in out, f"plumbing command leaked into --help: {hidden_cmd}"


def test_hidden_commands_still_callable(runner: CliRunner):
    """hidden=True must not break the hook-wired entry points."""
    for cmd in ("ingest-session", "ingest-live", "context", "backfill-episodes", "mcp-server"):
        result = runner.invoke(app, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help failed"


def test_reanalyze_is_removed_at_1_0(runner: CliRunner, tmp_path: Path, monkeypatch):
    """`reanalyze` was a deprecated alias through 0.13; `analyze --all` survives."""
    monkeypatch.setenv("HOME", str(tmp_path))
    result = runner.invoke(app, ["reanalyze", "--data-dir", str(tmp_path / "store")])
    assert result.exit_code == 2

    analyze = runner.invoke(app, ["analyze", "--help"])
    assert analyze.exit_code == 0
    assert "--all" in plain(analyze.stdout)


def test_stats_splits_low_confidence_noise(runner: CliRunner, tmp_path: Path, monkeypatch):
    """Resolved rate must exclude low-confidence fixless extractions."""
    from longhand.storage.store import LonghandStore

    monkeypatch.setenv("HOME", str(tmp_path))
    data_dir = tmp_path / "store"
    store = LonghandStore(data_dir=data_dir)

    rows = [
        # (id, confidence, fix_event_id, status)
        ("ep-resolved", 0.9, "fix-1", "resolved"),
        ("ep-real-open", 0.8, None, "unresolved"),
        ("ep-noise", 0.2, None, "unresolved"),
    ]
    with store.sqlite.connect() as conn:
        for ep_id, conf, fix, status in rows:
            conn.execute(
                "INSERT INTO episodes (episode_id, session_id, started_at, ended_at, "
                "fix_event_id, confidence, status) VALUES (?, 's1', 't0', 't1', ?, ?, ?)",
                (ep_id, fix, conf, status),
            )

    s = store.stats()
    assert s["episodes"] == 3
    assert s["resolved_episodes"] == 1
    assert s["low_confidence_episodes"] == 1
    # rate over substantive episodes (3 - 1 noise = 2): 1/2 = 50%
    assert s["resolved_rate_pct"] == 50

    out = plain(runner.invoke(app, ["stats", "--data-dir", str(data_dir)]).stdout)
    assert "low-confidence" in out
    assert "50%" in out
    assert "excludes low-confidence" in out
