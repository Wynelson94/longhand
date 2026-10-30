"""CLI tests for ``longhand backup``: the consent flow, the run, status, auto, disable.

Everything runs through Typer's CliRunner against a bare repository under
``tmp_path``. CliRunner's stdin is not a TTY, so the typed confirmations are
exercised by patching the one helper that asks whether stdin is a terminal.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from longhand.backup import gitrepo
from longhand.backup.gitrepo import GitRepo, git_executable
from longhand.backup.service import BackupConfig
from longhand.cli import app, backup_commands

TOKEN = "ghp_" + "A" * 36

pytestmark = [
    pytest.mark.skipif(git_executable() is None, reason="git is not installed"),
    pytest.mark.usefixtures("isolated_git_config"),
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data dir under tmp_path; HOME is redirected so ~/.longhand never leaks in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "lh"))
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    return tmp_path / "lh"


def _session_file(tmp_path: Path, session_id: str, text: str) -> Path:
    path = tmp_path / f"{session_id}.jsonl"
    entries: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": f"{session_id}-user-1",
            "parentUuid": None,
            "sessionId": session_id,
            "timestamp": "2026-05-01T09:00:00.000Z",
            "cwd": "/Users/tester/test-project",
            "gitBranch": "main",
            "isSidechain": False,
            "message": {"role": "user", "content": text},
        },
        {
            "type": "assistant",
            "uuid": f"{session_id}-asst-1",
            "parentUuid": f"{session_id}-user-1",
            "sessionId": session_id,
            "timestamp": "2026-05-01T09:00:01.000Z",
            "cwd": "/Users/tester/test-project",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": "Noted."}],
            },
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _ingest_one(data_dir: Path, tmp_path: Path, session_id: str, text: str) -> None:
    from longhand.parser import JSONLParser
    from longhand.storage import LonghandStore

    parser = JSONLParser(_session_file(tmp_path, session_id, text))
    events = list(parser.parse_events())
    session = parser.build_session(events)
    LonghandStore(data_dir=data_dir).ingest_session(session, events, run_analysis=False)


def _bare_commits(bare: Path) -> int:
    git = git_executable()
    assert git is not None
    proc = subprocess.run(
        [git, "--git-dir", str(bare), "rev-list", "--count", "main"],
        capture_output=True,
        text=True,
    )
    return int(proc.stdout.strip()) if proc.returncode == 0 else 0


def _init_args(data_dir: Path, remote: str, *extra: str) -> list[str]:
    return ["backup", "init", "--remote", remote, "--data-dir", str(data_dir), *extra]


@pytest.fixture
def enabled_env(data_dir: Path, tmp_path: Path, bare_remote: Path) -> SimpleNamespace:
    """Backups enabled against the bare remote, one session with a token in the store."""
    repo = GitRepo.init(data_dir / "backup", str(bare_remote))
    BackupConfig(enabled=True, remote=str(bare_remote), path=str(repo.path)).save(data_dir)
    _ingest_one(data_dir, tmp_path, "s-one", f"use {TOKEN} for the API call")
    return SimpleNamespace(data_dir=data_dir, bare=bare_remote, repo=repo, tmp=tmp_path)


# ─── init: the consent flow ───────────────────────────────────────────────────


def test_backup_init_refuses_without_tty_or_yes(
    runner: CliRunner, data_dir: Path, tmp_path: Path, bare_remote: Path
) -> None:
    _ingest_one(data_dir, tmp_path, "s-one", f"use {TOKEN} for the API call")

    result = runner.invoke(app, _init_args(data_dir, str(bare_remote)))

    assert result.exit_code == 1, result.output
    assert "Re-run with --yes" in result.output
    assert "Nothing is uploaded until you type yes." in result.output
    assert "Sessions:      1 (1 Claude Code, 0 Codex)" in result.output
    assert "github_token: 1 (1 session" in result.output
    assert "redact.enabled = False" in result.output
    assert "Encryption:    none" in result.output
    assert BackupConfig.load(data_dir).enabled is False
    assert not (data_dir / "backup").exists()


def test_backup_init_with_yes_enables_and_creates_repo(
    runner: CliRunner, data_dir: Path, bare_remote: Path
) -> None:
    result = runner.invoke(app, _init_args(data_dir, str(bare_remote), "--yes"))

    assert result.exit_code == 0, result.output
    assert "Backups enabled" in result.output
    config = json.loads((data_dir / "config.json").read_text())["backup"]
    assert config == {
        "enabled": True,
        "remote": str(bare_remote),
        "path": str(data_dir / "backup"),
        "auto": False,
    }
    repo = GitRepo(data_dir / "backup")
    assert repo.exists()
    assert repo.run("remote", "get-url", "origin").stdout.strip() == str(bare_remote)
    assert repo.current_branch() == "main"
    assert _bare_commits(bare_remote) == 0  # init uploads nothing


def test_backup_init_typed_yes_and_private(
    runner: CliRunner, data_dir: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(backup_commands, "_stdin_is_tty", lambda: True)

    refused = runner.invoke(app, _init_args(data_dir, str(bare_remote)), input="no\n")
    assert refused.exit_code == 1, refused.output
    assert "Aborted" in refused.output
    assert BackupConfig.load(data_dir).enabled is False

    not_private = runner.invoke(app, _init_args(data_dir, str(bare_remote)), input="yes\nmaybe\n")
    assert not_private.exit_code == 1, not_private.output
    assert BackupConfig.load(data_dir).enabled is False

    accepted = runner.invoke(app, _init_args(data_dir, str(bare_remote)), input="yes\nprivate\n")
    assert accepted.exit_code == 0, accepted.output
    assert BackupConfig.load(data_dir).enabled is True


def test_backup_init_refuses_public_github(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitrepo, "github_visibility", lambda owner_repo, **kw: "public")

    result = runner.invoke(app, _init_args(data_dir, "https://github.com/o/r.git", "--yes"))

    assert result.exit_code == 1, result.output
    assert "public" in result.output.lower()
    assert not (data_dir / "config.json").exists()
    assert not (data_dir / "backup").exists()


def test_backup_init_accepts_private_github_without_cloning_when_empty(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitrepo, "github_visibility", lambda owner_repo, **kw: "private")
    monkeypatch.setattr(GitRepo, "remote_has_commits", staticmethod(lambda remote, **kw: False))

    result = runner.invoke(app, _init_args(data_dir, "https://github.com/o/r.git", "--yes"))

    assert result.exit_code == 0, result.output
    assert "private" in result.output
    repo = GitRepo(data_dir / "backup")
    assert repo.exists()
    assert repo.run("remote", "get-url", "origin").stdout.strip() == "https://github.com/o/r.git"
    assert BackupConfig.load(data_dir).remote == "https://github.com/o/r.git"


def test_backup_init_clones_when_the_remote_has_commits(
    runner: CliRunner, data_dir: Path, tmp_path: Path, bare_remote: Path
) -> None:
    seed = GitRepo.init(tmp_path / "seed", str(bare_remote))
    (seed.path / "README.md").write_text("seeded\n")
    seed.add_all()
    seed.commit("seed")
    seed.push()

    result = runner.invoke(app, _init_args(data_dir, str(bare_remote), "--yes"))

    assert result.exit_code == 0, result.output
    assert (data_dir / "backup" / "README.md").read_text() == "seeded\n"


def test_backup_init_adopts_an_existing_clone(
    runner: CliRunner, data_dir: Path, bare_remote: Path
) -> None:
    GitRepo.init(data_dir / "backup", str(bare_remote))

    result = runner.invoke(app, _init_args(data_dir, str(bare_remote), "--yes"))

    assert result.exit_code == 0, result.output
    assert "existing clone" in result.output
    assert BackupConfig.load(data_dir).enabled is True


def test_backup_init_rejects_bad_remote(runner: CliRunner, data_dir: Path) -> None:
    result = runner.invoke(app, _init_args(data_dir, "ext::sh -c id", "--yes"))

    assert result.exit_code == 1, result.output
    assert "not a supported remote" in result.output
    assert not (data_dir / "config.json").exists()


def test_backup_init_create_calls_gh(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []
    monkeypatch.setattr(
        gitrepo, "github_create_private", lambda owner_repo, **kw: created.append(owner_repo)
    )
    monkeypatch.setattr(gitrepo, "github_visibility", lambda owner_repo, **kw: "private")
    monkeypatch.setattr(GitRepo, "remote_has_commits", staticmethod(lambda remote, **kw: False))

    result = runner.invoke(app, _init_args(data_dir, "https://github.com/o/r", "--create", "--yes"))

    assert result.exit_code == 0, result.output
    assert created == ["o/r"]

    elsewhere = runner.invoke(app, _init_args(data_dir, "/tmp/elsewhere.git", "--create", "--yes"))
    assert elsewhere.exit_code == 1, elsewhere.output
    assert "needs a github.com remote" in " ".join(elsewhere.output.split())


def test_backup_init_without_git_refuses(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gitrepo, "git_executable", lambda: None)

    result = runner.invoke(app, _init_args(data_dir, "/tmp/r.git", "--yes"))

    assert result.exit_code == 1, result.output
    assert "git is not installed" in result.output


# ─── The run ──────────────────────────────────────────────────────────────────


def test_backup_run_without_init_hints_init(runner: CliRunner, data_dir: Path) -> None:
    result = runner.invoke(app, ["backup", "--data-dir", str(data_dir)])

    assert result.exit_code == 1, result.output
    assert "longhand backup init" in result.output


def test_backup_run_dry_run_and_real(runner: CliRunner, enabled_env: SimpleNamespace) -> None:
    dry = runner.invoke(app, ["backup", "--dry-run", "--data-dir", str(enabled_env.data_dir)])
    assert dry.exit_code == 0, dry.output
    assert "1 to add" in dry.output
    assert "github_token: 1" in dry.output
    assert _bare_commits(enabled_env.bare) == 0

    real = runner.invoke(app, ["backup", "--data-dir", str(enabled_env.data_dir)])
    assert real.exit_code == 0, real.output
    assert "pushed" in real.output
    assert "1 added" in real.output
    assert "github_token: 1" in real.output
    assert TOKEN not in real.output
    assert _bare_commits(enabled_env.bare) == 1

    again = runner.invoke(app, ["backup", "--data-dir", str(enabled_env.data_dir)])
    assert again.exit_code == 0, again.output
    assert "0 added" in again.output
    assert _bare_commits(enabled_env.bare) == 1


def test_backup_run_no_push_and_push_failure_exit_codes(
    runner: CliRunner, enabled_env: SimpleNamespace
) -> None:
    local = runner.invoke(app, ["backup", "--no-push", "--data-dir", str(enabled_env.data_dir)])
    assert local.exit_code == 0, local.output
    assert "not pushed" in local.output
    assert _bare_commits(enabled_env.bare) == 0

    gone = enabled_env.tmp / "gone.git"
    enabled_env.repo.run("remote", "set-url", "origin", "--", str(gone))
    BackupConfig(enabled=True, remote=str(gone), path=str(enabled_env.repo.path)).save(
        enabled_env.data_dir
    )
    failed = runner.invoke(app, ["backup", "--data-dir", str(enabled_env.data_dir)])
    assert failed.exit_code == 1, failed.output
    assert "push failed" in failed.output


def test_backup_no_mask_refuses_without_tty(
    runner: CliRunner, enabled_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = runner.invoke(app, ["backup", "--no-mask", "--data-dir", str(enabled_env.data_dir)])
    assert result.exit_code == 1, result.output
    assert "verbatim" in result.output
    assert _bare_commits(enabled_env.bare) == 0

    monkeypatch.setattr(backup_commands, "_stdin_is_tty", lambda: True)
    wrong = runner.invoke(
        app, ["backup", "--no-mask", "--data-dir", str(enabled_env.data_dir)], input="yes\n"
    )
    assert wrong.exit_code == 1, wrong.output
    assert _bare_commits(enabled_env.bare) == 0

    typed = runner.invoke(
        app, ["backup", "--no-mask", "--data-dir", str(enabled_env.data_dir)], input="verbatim\n"
    )
    assert typed.exit_code == 0, typed.output
    assert _bare_commits(enabled_env.bare) == 1
    transcript = (
        enabled_env.repo.path / "sessions" / "claude" / "2026" / "05" / "s-one" / "transcript.jsonl"
    )
    assert TOKEN in transcript.read_text(encoding="utf-8")


# ─── status / auto / disable ──────────────────────────────────────────────────


def test_backup_status_when_disabled(runner: CliRunner, data_dir: Path) -> None:
    result = runner.invoke(app, ["backup", "status", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert "off" in result.output
    assert "longhand backup init" in result.output


def test_backup_status_and_auto_and_disable(
    runner: CliRunner, enabled_env: SimpleNamespace
) -> None:
    data_dir = str(enabled_env.data_dir)

    status = runner.invoke(app, ["backup", "status", "--data-dir", data_dir])
    assert status.exit_code == 0, status.output
    assert str(enabled_env.bare) in status.output
    assert "never" in status.output

    on = runner.invoke(app, ["backup", "auto", "on", "--data-dir", data_dir])
    assert on.exit_code == 0, on.output
    assert BackupConfig.load(enabled_env.data_dir).auto is True

    sideways = runner.invoke(app, ["backup", "auto", "sideways", "--data-dir", data_dir])
    assert sideways.exit_code == 1, sideways.output

    off = runner.invoke(app, ["backup", "auto", "off", "--data-dir", data_dir])
    assert off.exit_code == 0, off.output
    assert BackupConfig.load(enabled_env.data_dir).auto is False

    disable = runner.invoke(app, ["backup", "disable", "--data-dir", data_dir])
    assert disable.exit_code == 0, disable.output
    assert "clone stays" in disable.output
    assert BackupConfig.load(enabled_env.data_dir).enabled is False
    assert enabled_env.repo.exists()

    run = runner.invoke(app, ["backup", "--data-dir", data_dir])
    assert run.exit_code == 1, run.output

    auto_off = runner.invoke(app, ["backup", "auto", "on", "--data-dir", data_dir])
    assert auto_off.exit_code == 1, auto_off.output


def test_backup_help_lists_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(app, ["backup", "--help"])

    assert result.exit_code == 0, result.output
    for name in ("init", "status", "auto", "disable", "--dry-run", "--no-mask"):
        assert name in result.output


# ─── config namespace ─────────────────────────────────────────────────────────


def test_config_set_accepts_backup_namespace(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / ".longhand"))

    result = runner.invoke(app, ["config", "--set", "backup.auto=true"])
    assert result.exit_code == 0, result.output
    config = json.loads((tmp_path / ".longhand" / "config.json").read_text())
    assert config["backup"]["auto"] is True

    shown = runner.invoke(app, ["config", "--show"])
    assert shown.exit_code == 0, shown.output
    assert "backup.auto" in shown.output
    assert "backup.enabled" in shown.output


# ─── reconcile hook and doctor row (Task 8) ───────────────────────────────────


def test_reconcile_fix_calls_maybe_auto_backup(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longhand.recall import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [])
    calls: list[Path] = []

    def fake(sqlite: Any, dd: Path, **kwargs: Any) -> None:
        calls.append(dd)
        return None

    monkeypatch.setattr("longhand.backup.service.maybe_auto_backup", fake)

    plain = runner.invoke(app, ["reconcile", "--data-dir", str(data_dir)])
    assert plain.exit_code == 0, plain.output
    assert calls == []

    fixed = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(data_dir)])
    assert fixed.exit_code == 0, fixed.output
    assert calls == [data_dir]  # runs on every --fix pass, even when nothing needed re-ingesting


def test_reconcile_fix_prints_the_auto_backup_outcome(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longhand.backup.service import RunReport
    from longhand.recall import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [])
    monkeypatch.setattr(
        "longhand.backup.service.maybe_auto_backup",
        lambda sqlite, dd, **kw: RunReport(planned=2, added=1, grown=1, commits=1, pushed=True),
    )

    result = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert (
        "Backup" in result.output
        and "1 added, 1 updated" in result.output
        and "pushed" in result.output
    )


def test_reconcile_fix_survives_a_broken_backup_hook(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longhand.recall import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [])

    def boom(sqlite: Any, dd: Path, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("longhand.backup.service.maybe_auto_backup", boom)

    result = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output


def test_doctor_shows_backup_row(runner: CliRunner, data_dir: Path) -> None:
    off = runner.invoke(app, ["doctor", "--json"])
    assert off.exit_code == 0, off.output
    assert json.loads(off.stdout)["Backup"] == "off"

    BackupConfig(enabled=True, remote="/r.git").save(data_dir)
    on = runner.invoke(app, ["doctor", "--json"])
    assert on.exit_code == 0, on.output
    assert json.loads(on.stdout)["Backup"] == "on · never pushed"


def test_reconcile_fix_prints_a_bracketed_backup_error_safely(
    runner: CliRunner, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from longhand.backup.service import RunReport
    from longhand.recall import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [])
    monkeypatch.setattr(
        "longhand.backup.service.maybe_auto_backup",
        lambda sqlite, dd, **kw: RunReport(commits=1, error="push failed: [remote rejected] x"),
    )

    result = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(data_dir)])

    assert result.exit_code == 0, result.output
    assert "[remote rejected]" in result.output


# ─── Review fixes I2 / I4 / I5 ────────────────────────────────────────────────


def test_config_set_refuses_backup_keys_that_init_owns(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / ".longhand"))
    for key in ("backup.enabled=true", "backup.remote=/r.git", "backup.path=/p"):
        result = runner.invoke(app, ["config", "--set", key])
        assert result.exit_code == 0, result.output
        assert "backup init" in " ".join(result.output.split())
    assert not (tmp_path / ".longhand" / "config.json").exists()

    result = runner.invoke(app, ["config", "--set", "backup.auto=true"])
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / ".longhand" / "config.json").read_text())["backup"] == {
        "auto": True
    }


def test_backup_run_refuses_a_clone_with_another_origin(
    runner: CliRunner, enabled_env: SimpleNamespace
) -> None:
    enabled_env.repo.run("remote", "set-url", "origin", "--", str(enabled_env.tmp / "else.git"))

    result = runner.invoke(app, ["backup", "--data-dir", str(enabled_env.data_dir)])

    assert result.exit_code == 1, result.output
    assert "not backup.remote" in result.output
    assert "longhand backup init" in result.output
    assert _bare_commits(enabled_env.bare) == 0


def test_backup_init_refuses_a_path_holding_another_repository(
    runner: CliRunner, data_dir: Path, tmp_path: Path, bare_remote: Path
) -> None:
    project = GitRepo.init(data_dir / "backup", str(tmp_path / "elsewhere.git"))
    (project.path / "secret-notes.md").write_text("not a backup\n")
    project.add_all()
    project.commit("my project")

    result = runner.invoke(app, _init_args(data_dir, str(bare_remote), "--yes"))

    assert result.exit_code == 1, result.output
    assert "not a Longhand backup" in result.output
    assert BackupConfig.load(data_dir).enabled is False
    assert project.run("remote", "get-url", "origin").stdout.strip() == str(
        tmp_path / "elsewhere.git"
    )


def test_backup_init_refuses_a_non_empty_directory(
    runner: CliRunner, data_dir: Path, bare_remote: Path
) -> None:
    (data_dir / "backup").mkdir(parents=True)
    (data_dir / "backup" / "photo.jpg").write_bytes(b"\xff\xd8")

    result = runner.invoke(app, _init_args(data_dir, str(bare_remote), "--yes"))

    assert result.exit_code == 1, result.output
    assert "not empty" in result.output
    assert not (data_dir / "backup" / ".git").exists()
    assert BackupConfig.load(data_dir).enabled is False
