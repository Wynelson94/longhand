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


# ─── reconcile ──────────────────────────────────────────────────────────────


def test_cli_reconcile_help(runner: CliRunner):
    result = runner.invoke(app, ["reconcile", "--help"])
    assert result.exit_code == 0
    assert "disk" in result.stdout.lower() or "sessions table" in result.stdout.lower()


def test_cli_reconcile_reports_missing_and_fixes(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_session_file: Path,
):
    """reconcile must detect a JSONL on disk that isn't in the sessions table,
    then re-ingest it when --fix is passed.
    """
    from longhand.recall import reconcile as reconcile_mod
    from longhand.storage import LonghandStore

    data_dir = tmp_path / "longhand"
    store = LonghandStore(data_dir=data_dir)

    # Point discover_sessions at exactly our sample file. The reconcile core
    # was factored into longhand.recall.reconcile in v0.8.1 so the patch lives
    # there now (the CLI is a thin display wrapper).
    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [sample_session_file])

    # First pass: sample session is on disk but never ingested → "missing".
    result = runner.invoke(app, ["reconcile", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    assert "1 missing" in result.stdout
    assert "0 fully indexed" in result.stdout

    # --fix should ingest it.
    result = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    assert "Re-ingested 1" in result.stdout

    # Session row should now exist.
    with store.sqlite.connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE transcript_path = ?",
            (str(sample_session_file),),
        ).fetchone()[0]
    assert count == 1

    # Third pass: nothing to fix.
    result = runner.invoke(app, ["reconcile", "--data-dir", str(data_dir)])
    assert result.exit_code == 0
    assert "1 fully indexed" in result.stdout
    assert "0 missing" in result.stdout


def test_cli_reconcile_detects_null_project_rows(
    runner: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_session_file: Path,
):
    """reconcile must flag rows where project_id IS NULL so they can be re-analyzed."""
    from longhand.parser import JSONLParser
    from longhand.recall import reconcile as reconcile_mod
    from longhand.storage import LonghandStore

    data_dir = tmp_path / "longhand"
    store = LonghandStore(data_dir=data_dir)

    # Ingest the sample without analysis so project_id stays NULL.
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)

    with store.sqlite.connect() as conn:
        pid = conn.execute(
            "SELECT project_id FROM sessions WHERE transcript_path = ?",
            (str(sample_session_file),),
        ).fetchone()[0]
    assert pid is None, "session should be ingested without project_id"

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [sample_session_file])

    result = runner.invoke(app, ["reconcile", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.stdout
    assert "1 ingested but project_id IS NULL" in result.stdout


# ─── status consolidation + deprecation aliases (v0.13) ─────────────────────
#
# `status` is the single resume command, git-status shaped: bare = recent
# digest, positional = project deep status, --session = one session's tail.
# recap/continue/patterns become hidden delegating aliases through 0.13 and
# are deleted at 1.0.


def _seed_session(sample_session_file: Path, data_dir: Path) -> str:
    """Ingest the sample session; return its session_id."""
    from longhand.setup_commands import ingest_single_session
    from longhand.storage import LonghandStore

    ingest_single_session(str(sample_session_file), data_dir=str(data_dir), run_analysis=False)
    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        return conn.execute("SELECT session_id FROM sessions").fetchone()[0]


def test_status_bare_shows_recent_digest(
    runner: CliRunner, sample_session_file: Path, tmp_path: Path
):
    data_dir = tmp_path / "lh"
    sid = _seed_session(sample_session_file, data_dir)

    result = runner.invoke(app, ["status", "--days", "3650", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert sid[:8] in result.stdout


def test_status_unknown_project_still_exits_one(runner: CliRunner, tmp_path: Path):
    result = runner.invoke(
        app, ["status", "definitely-no-such-project", "--data-dir", str(tmp_path / "lh")]
    )
    assert result.exit_code == 1
    assert "No project matching" in result.stdout


def test_status_session_mode_tails_events(
    runner: CliRunner, sample_session_file: Path, tmp_path: Path
):
    data_dir = tmp_path / "lh"
    sid = _seed_session(sample_session_file, data_dir)

    result = runner.invoke(app, ["status", "--session", sid[:8], "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert sid in result.stdout  # full id in the session header
    assert "Last" in result.stdout  # the event tail rendered


def test_status_rejects_project_and_session_together(runner: CliRunner, tmp_path: Path):
    result = runner.invoke(
        app,
        ["status", "someproj", "--session", "abc", "--data-dir", str(tmp_path / "lh")],
    )
    assert result.exit_code == 2


def test_status_json_digest(runner: CliRunner, sample_session_file: Path, tmp_path: Path):
    data_dir = tmp_path / "lh"
    sid = _seed_session(sample_session_file, data_dir)

    result = runner.invoke(app, ["status", "--days", "3650", "--json", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "digest"
    assert any(s["session_id"] == sid for s in payload["sessions"])


@pytest.mark.parametrize("removed", ["recap", "continue", "patterns", "reanalyze"])
def test_deprecated_aliases_are_gone_at_1_0(runner: CliRunner, tmp_path: Path, removed: str):
    """The 0.13 deprecation window closed — these four no longer exist.

    Typer exits 2 ("No such command") rather than running anything. The
    survivors are `status` (recap/continue), `recall` (patterns) and
    `analyze --all` (reanalyze).
    """
    result = runner.invoke(app, [removed, "--data-dir", str(tmp_path / "lh")])

    assert result.exit_code == 2, result.output


def test_removed_commands_are_not_registered():
    names = {
        (info.name or info.callback.__name__.replace("_", "-")) for info in app.registered_commands
    }
    assert not ({"recap", "continue", "patterns", "reanalyze"} & names)


def test_hook_config_honors_data_dir_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from longhand.setup_commands import _load_hook_config

    cfg_dir = tmp_path / "env"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"hook": {"max_episodes": 7}}))
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(cfg_dir))
    monkeypatch.setenv("HOME", str(tmp_path))  # the real ~/.longhand must not leak in

    assert _load_hook_config()["max_episodes"] == 7


def test_doctor_json_reports_data_dir_and_source(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "lh"))
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert str(tmp_path / "lh") in payload["Data directory"]
    assert "LONGHAND_DATA_DIR" in payload["Data directory"]


def test_reconciler_plist_bakes_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """launchd jobs do not inherit shell env — the plist must carry the
    resolved data dir explicitly or a relocated store gets missed."""
    from longhand.setup_commands import _reconciler_plist_xml

    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "relocated"))
    xml = _reconciler_plist_xml("/usr/local/bin/longhand", tmp_path / "reconcile.log")

    assert "LONGHAND_DATA_DIR" in xml
    assert str(tmp_path / "relocated") in xml


# ─── ingest-lock coverage for the heavy writers ──────────────────────────────
#
# analyze / reattribute --fix / redact --apply write into the same SQLite +
# Chroma stores the ingest lock serializes. They wait-claim the lock (humans
# want "run when the ingest finishes", not "try again later"), abort with
# exit 1 on timeout, and never touch the lock in their read-only modes.


def test_analyze_aborts_when_ingest_lock_stays_busy(
    runner: CliRunner, sample_session_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from longhand.recall import project_fallback
    from longhand.setup_commands import ingest_single_session
    from longhand.storage.store import LonghandStore

    data_dir = tmp_path / "lh"
    ingest_single_session(str(sample_session_file), data_dir=str(data_dir), run_analysis=False)

    # A busy holder that never lets go — don't actually poll for 15s in a test.
    monkeypatch.setattr(project_fallback, "claim_ingest_lock_with_wait", lambda store, **kw: False)
    analyzed: list[int] = []
    monkeypatch.setattr(
        LonghandStore, "analyze_session", lambda self, *a, **k: analyzed.append(1) or {}
    )

    result = runner.invoke(app, ["analyze", "--all", "--data-dir", str(data_dir)])

    assert result.exit_code == 1
    assert "ingest" in result.output.lower()
    assert analyzed == []  # aborted before touching the store


def test_analyze_claims_and_releases_the_lock(
    runner: CliRunner, sample_session_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from longhand.recall import project_fallback
    from longhand.setup_commands import ingest_single_session
    from longhand.storage.store import LonghandStore

    data_dir = tmp_path / "lh"
    ingest_single_session(str(sample_session_file), data_dir=str(data_dir), run_analysis=False)
    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        sid = conn.execute("SELECT session_id FROM sessions").fetchone()[0]

    calls: list[str] = []
    monkeypatch.setattr(
        project_fallback,
        "claim_ingest_lock_with_wait",
        lambda store, **kw: calls.append("claim") or True,
    )
    monkeypatch.setattr(
        project_fallback, "release_ingest_lock", lambda store: calls.append("release")
    )
    monkeypatch.setattr(LonghandStore, "analyze_session", lambda self, *a, **k: {"episodes": 0})

    result = runner.invoke(app, ["analyze", "--session", sid[:8], "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert calls == ["claim", "release"]


def test_reattribute_fix_aborts_when_ingest_lock_stays_busy(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from longhand.recall import project_fallback
    from longhand.storage.store import LonghandStore

    monkeypatch.setattr(project_fallback, "claim_ingest_lock_with_wait", lambda store, **kw: False)
    moved: list[int] = []
    monkeypatch.setattr(
        LonghandStore,
        "reattribute_sessions",
        lambda self, apply=False: moved.append(1) or {},
    )

    result = runner.invoke(app, ["reattribute", "--fix", "--data-dir", str(tmp_path / "lh")])

    assert result.exit_code == 1
    assert moved == []


def test_reattribute_dry_run_never_touches_the_lock(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from longhand.recall import project_fallback

    claims: list[int] = []
    monkeypatch.setattr(
        project_fallback,
        "claim_ingest_lock_with_wait",
        lambda store, **kw: claims.append(1) or True,
    )

    result = runner.invoke(app, ["reattribute", "--data-dir", str(tmp_path / "lh")])

    assert result.exit_code == 0, result.output
    assert claims == []


def test_redact_apply_aborts_when_ingest_lock_stays_busy(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from longhand.recall import project_fallback

    monkeypatch.setattr(project_fallback, "claim_ingest_lock_with_wait", lambda store, **kw: False)

    result = runner.invoke(app, ["redact", "--apply", "--yes", "--data-dir", str(tmp_path / "lh")])

    assert result.exit_code == 1


def test_redact_scan_never_touches_the_lock(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from longhand.recall import project_fallback

    claims: list[int] = []
    monkeypatch.setattr(
        project_fallback,
        "claim_ingest_lock_with_wait",
        lambda store, **kw: claims.append(1) or True,
    )

    result = runner.invoke(app, ["redact", "--data-dir", str(tmp_path / "lh")])

    assert result.exit_code == 0, result.output
    assert claims == []


def test_redact_apply_claims_lock_only_after_confirmation(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Declining the irreversibility prompt must abort before any lock work —
    users shouldn't hold the writers' lock while staring at a y/N prompt."""
    from longhand.recall import project_fallback

    claims: list[int] = []
    monkeypatch.setattr(
        project_fallback,
        "claim_ingest_lock_with_wait",
        lambda store, **kw: claims.append(1) or True,
    )

    result = runner.invoke(
        app, ["redact", "--apply", "--data-dir", str(tmp_path / "lh")], input="n\n"
    )

    assert result.exit_code == 0
    assert "Aborted" in result.output
    assert claims == []


# ─── doctor hook-error visibility ───────────────────────────────────────────


def test_hook_errors_status_green_when_no_errors(tmp_path: Path):
    from longhand.setup_commands import _hook_errors_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    status = _hook_errors_status(store)
    assert "green" in status
    assert "none" in status


def test_hook_errors_status_counts_recent_and_ignores_old(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    from longhand.setup_commands import _hook_errors_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    logs = store.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).date()
    recent = logs / f"hook-errors-{today.isoformat()}.log"
    recent.write_text(
        "2026-07-11T18:00:00+00:00 ingest-session missing-transcript: /tmp/gone.jsonl\n"
        "2026-07-11T18:05:00+00:00 ingest-session ingest-failed: boom\n"
    )
    old = logs / f"hook-errors-{(today - timedelta(days=30)).isoformat()}.log"
    old.write_text("ancient failure, outside the window\n")
    (logs / "hook-errors-not-a-date.log").write_text("garbage name, must not crash\n")

    status = _hook_errors_status(store)
    assert "yellow" in status
    assert "2 in the last 7 days" in status
    assert "hook-errors-*.log" in status


# ─── doctor hook-error remedy is class-aware (Promise 5) ─────────────────────
#
# reconcile enumerates from DISK. A transcript that never landed is invisible
# to it forever, so "run reconcile --fix" is a no-op for that class. Over the
# v0.13 bake (2026-07-11..08-12) 21 of 23 real hook errors were exactly that
# class — the row recommended a no-op for 91% of what it reported.


def _write_hook_log(store, *lines: str) -> None:
    from datetime import datetime, timezone

    logs = store.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    (logs / f"hook-errors-{today.isoformat()}.log").write_text("".join(f"{ln}\n" for ln in lines))


def test_hook_errors_unhealable_class_does_not_recommend_reconcile(tmp_path: Path):
    from longhand.setup_commands import _hook_errors_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    _write_hook_log(
        store,
        "2026-08-11T17:10:51+00:00 ingest-session missing-transcript: /tmp/never-landed.jsonl",
        "2026-08-11T21:51:19+00:00 ingest-session missing-transcript: /tmp/also-gone.jsonl",
    )

    status = _hook_errors_status(store)

    assert "2 in the last 7 days" in status
    assert "missing-transcript" in status
    assert "nothing to heal" in status
    assert "reconcile" not in status  # the no-op recommendation is gone


def test_hook_errors_healable_class_keeps_reconcile_advice(tmp_path: Path):
    from longhand.setup_commands import _hook_errors_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    _write_hook_log(
        store,
        "2026-07-25T00:07:46+00:00 ingest-session ingest-failed: a.jsonl: RuntimeError: boom",
    )

    status = _hook_errors_status(store)

    assert "1 in the last 7 days" in status
    assert "reconcile" in status  # genuinely healable — keep the advice


def test_hook_errors_mixed_classes_split_the_remedy(tmp_path: Path):
    """The live shape: a healable minority alongside an unhealable majority."""
    from longhand.setup_commands import _hook_errors_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    _write_hook_log(
        store,
        "2026-08-11T17:10:51+00:00 ingest-session missing-transcript: /tmp/gone.jsonl",
        "2026-08-11T17:11:51+00:00 ingest-session missing-transcript: /tmp/gone2.jsonl",
        "2026-07-25T00:07:46+00:00 ingest-session ingest-failed: a.jsonl: RuntimeError: boom",
    )

    status = _hook_errors_status(store)

    assert "3 in the last 7 days" in status
    assert "1 ingest-failed" in status
    assert "2 missing-transcript" in status
    assert "reconcile" in status  # scoped to the 1 that it can actually fix


def test_hook_errors_unknown_class_never_claims_healability(tmp_path: Path):
    """Unparseable or new class tokens count, but must not inherit the advice."""
    from longhand.setup_commands import _hook_errors_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    _write_hook_log(
        store,
        "2026-08-11T17:10:51+00:00 ingest-session store-open-failed: OSError: disk gone",
        "a malformed line with no class token at all",
    )

    status = _hook_errors_status(store)

    assert "2 in the last 7 days" in status
    assert "reconcile" not in status


# ─── doctor transcript-format drift ──────────────────────────────────────────


def test_transcript_format_status_green_when_no_unknowns(tmp_path: Path):
    from longhand.setup_commands import _transcript_format_status
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    status = _transcript_format_status(store)
    assert "green" in status


def test_transcript_format_status_yellow_names_the_drifting_type(tmp_path: Path):
    from longhand.setup_commands import _transcript_format_status
    from longhand.storage import LonghandStore
    from longhand.timeutil import utcnow

    store = LonghandStore(data_dir=tmp_path / "longhand")
    recent = utcnow().isoformat()
    with store.sqlite.connect() as conn:
        for i in range(3):
            conn.execute(
                "INSERT INTO events (event_id, session_id, event_type, sequence,"
                " timestamp, content, raw_json) VALUES (?, 's-drift', 'unknown', ?, ?, '', ?)",
                (f"unk-{i}", i, recent, json.dumps({"type": "flux-capacitor"})),
            )
        # An old unknown outside the 30-day window must not count.
        conn.execute(
            "INSERT INTO events (event_id, session_id, event_type, sequence,"
            " timestamp, content, raw_json) VALUES ('unk-old', 's-drift', 'unknown',"
            " 99, '2020-01-01T00:00:00+00:00', '', ?)",
            (json.dumps({"type": "ancient"}),),
        )
        conn.commit()

    status = _transcript_format_status(store)
    assert "yellow" in status
    assert "flux-capacitor" in status
    assert "ancient" not in status
    assert "pip install -U longhand" in status


def test_transcript_format_status_ignores_dispositioned_types(tmp_path: Path):
    """Skip-set rows stored before the skip existed, and deliberately-preserved
    triaged types, are understood — alarming on them forever would train users
    to ignore the drift row."""
    from longhand.setup_commands import _transcript_format_status
    from longhand.storage import LonghandStore
    from longhand.timeutil import utcnow

    store = LonghandStore(data_dir=tmp_path / "longhand")
    recent = utcnow().isoformat()
    with store.sqlite.connect() as conn:
        for i, entry_type in enumerate(["attachment", "summary", "pr-link"]):
            conn.execute(
                "INSERT INTO events (event_id, session_id, event_type, sequence,"
                " timestamp, content, raw_json) VALUES (?, 's-known', 'unknown', ?, ?, '', ?)",
                (f"known-{i}", i, recent, json.dumps({"type": entry_type})),
            )
        conn.commit()

    status = _transcript_format_status(store)
    assert "green" in status


# ─── doctor freshness ──────────────────────────────────────────────────────


def test_freshness_status_green_when_all_recent_ingested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Every recent JSONL is in the sessions table → green."""
    from longhand import setup_commands
    from longhand.parser import JSONLParser
    from longhand.setup_commands import _freshness_status
    from longhand.storage import LonghandStore
    from tests.conftest import _line  # noqa: F401  (re-uses fixture helpers indirectly)

    # Make a minimal JSONL and ingest it.
    jsonl = tmp_path / "fresh.jsonl"
    jsonl.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "sessionId": "s-fresh",
                "timestamp": "2026-04-23T00:00:00Z",
                "cwd": str(tmp_path),
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
    )

    store = LonghandStore(data_dir=tmp_path / "longhand")
    parser = JSONLParser(jsonl)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)

    monkeypatch.setattr(setup_commands, "discover_sessions", lambda: [jsonl])
    status = _freshness_status(store)
    assert status is not None
    assert "green" in status
    assert "1/1 transcripts ingested" in status


def test_freshness_status_red_when_most_recent_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Fresh JSONLs exist on disk but none are in the sessions table → red."""
    from longhand import setup_commands
    from longhand.setup_commands import _freshness_status
    from longhand.storage import LonghandStore

    jsonls = []
    for i in range(4):
        p = tmp_path / f"miss-{i}.jsonl"
        p.write_text(json.dumps({"type": "user", "uuid": f"u{i}"}) + "\n")
        jsonls.append(p)

    store = LonghandStore(data_dir=tmp_path / "longhand")
    monkeypatch.setattr(setup_commands, "discover_sessions", lambda: jsonls)
    status = _freshness_status(store)
    assert status is not None
    assert "red" in status
    assert "reconcile --fix" in status
    assert "0/4" in status


def test_freshness_status_no_recent_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When no JSONLs are recent, freshness returns green with a neutral hint."""
    import os

    from longhand import setup_commands
    from longhand.setup_commands import _freshness_status
    from longhand.storage import LonghandStore

    old_jsonl = tmp_path / "old.jsonl"
    old_jsonl.write_text(json.dumps({"type": "user"}) + "\n")
    # Backdate mtime beyond the 7-day window.
    old_ts = old_jsonl.stat().st_mtime - 30 * 86400
    os.utime(old_jsonl, (old_ts, old_ts))

    store = LonghandStore(data_dir=tmp_path / "longhand")
    monkeypatch.setattr(setup_commands, "discover_sessions", lambda: [old_jsonl])
    status = _freshness_status(store)
    assert status is not None
    assert "green" in status
    assert "no recent Claude Code activity" in status


# ─── recall --json flag (R4) ────────────────────────────────────────────────


def test_cli_recall_json_flag_emits_valid_json(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`longhand recall "..." --json` must print a JSON object with the expected
    top-level shape — same keys the MCP tool exposes — so users can inspect
    what an agent sees.
    """
    # Use an isolated, empty data_dir so we don't touch ~/.longhand
    data_dir = tmp_path / "longhand"

    result = runner.invoke(
        app,
        ["recall", "nothing in this empty store", "--json", "--data-dir", str(data_dir)],
    )
    assert result.exit_code == 0, f"recall --json failed: {result.stdout}"

    payload = json.loads(result.stdout)
    assert "query" in payload
    assert "project_matches" in payload
    assert "episodes" in payload
    assert "segments" in payload
    assert "narrative" in payload
    # Artifacts key should be absent on an empty store (matches MCP behavior)
    assert "artifacts" not in payload


# ─── v0.12 DB hygiene ────────────────────────────────────────────────────────


def _seed_unknown_events(store, n: int) -> None:
    with store.sqlite.connect() as conn:
        for k in range(n):
            conn.execute(
                "INSERT INTO events (event_id, session_id, event_type, sequence,"
                " timestamp, content, raw_json) VALUES (?, 's-aux', 'unknown', ?,"
                " '2026-07-01T00:00:00Z', '', ?)",
                (f"unk-{k}", k, '{"type": "mode", "payload": "' + "x" * 200 + '"}'),
            )


def test_parser_skips_aux_entry_types(tmp_path: Path):
    """last-prompt/mode/permission-mode/attachment/ai-title/agent-name entries
    must not be stored as unknown events (they were the 3rd-largest event
    type on a real corpus); genuinely unrecognized types are still kept."""
    from longhand.parser import JSONLParser

    entries = [
        {"type": t, "uuid": f"u-{t}", "sessionId": "s1", "timestamp": "2026-07-01T00:00:00Z"}
        for t in ("last-prompt", "mode", "permission-mode", "attachment", "ai-title", "agent-name")
    ]
    entries.append(
        {
            "type": "some-future-type",
            "uuid": "u-future",
            "sessionId": "s1",
            "timestamp": "2026-07-01T00:00:00Z",
        }
    )
    f = tmp_path / "aux.jsonl"
    f.write_text("".join(json.dumps(e) + "\n" for e in entries))

    events = list(JSONLParser(f).parse_events())
    types = [e.event_type.value if hasattr(e.event_type, "value") else e.event_type for e in events]
    assert types == ["unknown"]  # only the genuinely unrecognized one survives
    assert events[0].event_id == "u-future"


def test_db_vacuum_prune_aux_removes_unknown_events(runner: CliRunner, tmp_path: Path):
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "lh")
    _seed_unknown_events(store, 5)

    result = runner.invoke(app, ["db", "vacuum", "--prune-aux", "--data-dir", str(tmp_path / "lh")])
    assert result.exit_code == 0, result.output
    assert "Pruned" in result.output and "5" in result.output
    with store.sqlite.connect() as conn:
        left = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='unknown'").fetchone()[0]
    assert left == 0
    lock = tmp_path / "lh" / ".ingest.lock"
    assert not lock.exists()  # released


def test_db_vacuum_defers_to_running_ingest(runner: CliRunner, tmp_path: Path):
    import os

    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "lh")
    _seed_unknown_events(store, 1)
    lock = tmp_path / "lh" / ".ingest.lock"
    lock.write_text(str(os.getppid()))  # alive holder

    result = runner.invoke(app, ["db", "vacuum", "--data-dir", str(tmp_path / "lh")])
    assert result.exit_code == 1
    with store.sqlite.connect() as conn:
        left = conn.execute("SELECT COUNT(*) FROM events WHERE event_type='unknown'").fetchone()[0]
    assert left == 1  # untouched
    lock.unlink()


def test_analyze_all_falls_back_to_events_table(runner: CliRunner, tmp_path: Path):
    """Sessions whose transcript rotated off disk still get re-analyzed —
    rebuilt from the events table instead of being counted as errors."""
    from longhand.parser import JSONLParser
    from longhand.storage import LonghandStore

    transcript = tmp_path / "rotated.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s-rot",
            "timestamp": "2026-07-01T00:00:01Z",
            "cwd": "/Users/tester/proj",
            "message": {"role": "user", "content": "fix the bug"},
        },
    ]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))

    store = LonghandStore(data_dir=tmp_path / "lh")
    parser = JSONLParser(transcript)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)

    transcript.unlink()  # rotate it away

    result = runner.invoke(app, ["analyze", "--all", "--data-dir", str(tmp_path / "lh")])
    assert result.exit_code == 0, result.output
    assert "Analyzed 1" in result.output
    assert "1 rebuilt from the events table" in result.output
    with store.sqlite.connect() as conn:
        outcome = conn.execute("SELECT COUNT(*) FROM session_outcomes").fetchone()[0]
    assert outcome == 1
