"""Tests for ``longhand restore``: the backup repository back into a fresh store.

The contract is the lines, not the rows: restore re-parses the exported
transcripts with the current parser. For the fixture sessions (every line
type is one the parser keeps) a re-export of the restored store reproduces
the backup's sha256 exactly, and a backup run from the restored store has
nothing to add.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from longhand.backup.export import (
    export_session,
    read_session_json,
    session_rel_dir,
    write_manifest,
)
from longhand.backup.gitrepo import GitRepo, git_executable
from longhand.backup.restore import (
    RestoreReport,
    copy_config_if_absent,
    iter_backup_sessions,
    materialize_transcript,
    restore_backup,
)
from longhand.backup.service import BackupConfig, run_backup
from longhand.backup.state import BackupState
from longhand.cli import app
from longhand.parser import JSONLParser

CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "codex_shapes" / "entries.jsonl"

pytestmark = [
    pytest.mark.skipif(git_executable() is None, reason="git is not installed"),
    pytest.mark.usefixtures("isolated_git_config"),
]


def _ingest(store: Any, path: Path) -> None:
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)


def _fresh_store(tmp_path: Path, name: str) -> Any:
    from longhand.storage import LonghandStore

    return LonghandStore(data_dir=tmp_path / name)


def _event_count(sqlite: Any, session_id: str) -> int:
    with sqlite.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
    return int(row[0])


def _bare_head(bare: Path) -> str:
    git = git_executable()
    assert git is not None
    proc = subprocess.run(
        [git, "--git-dir", str(bare), "rev-parse", "main"], capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _backup_sha(repo_root: Path, sqlite: Any, session_id: str) -> str:
    row = sqlite.get_session(session_id)
    assert row is not None
    return str(read_session_json(repo_root / Path(session_rel_dir(row)))["sha256"])


@pytest.fixture
def backed_up(
    temp_store: Any,
    tmp_path: Path,
    bare_remote: Path,
    sample_session_file: Path,
    multi_edit_session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Two Claude sessions backed up to the bare remote; the clone at tmp/backup."""
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    _ingest(temp_store, sample_session_file)
    _ingest(temp_store, multi_edit_session_file)
    (temp_store.data_dir / "config.json").write_text(json.dumps({"hook": {"enabled": True}}))
    repo = GitRepo.init(tmp_path / "backup", str(bare_remote))
    BackupConfig(enabled=True, remote=str(bare_remote), path=str(repo.path)).save(
        temp_store.data_dir
    )
    report = run_backup(temp_store.sqlite, temp_store.data_dir)
    assert report.added == 2 and report.pushed is True
    return SimpleNamespace(
        store=temp_store,
        repo=repo,
        bare=bare_remote,
        tmp=tmp_path,
        ids=["test-session-1", "edit-session"],
    )


# ─── Round trip ───────────────────────────────────────────────────────────────


def test_restore_round_trip_matches_export(backed_up: SimpleNamespace) -> None:
    fresh = _fresh_store(backed_up.tmp, "restored")

    report = restore_backup(fresh, backed_up.repo.path)

    assert isinstance(report, RestoreReport)
    assert (report.imported, report.skipped, report.errors) == (2, 0, [])
    assert report.events > 0
    for sid in backed_up.ids:
        row = fresh.sqlite.get_session(sid)
        assert row is not None, sid
        assert _event_count(fresh.sqlite, sid) == _event_count(backed_up.store.sqlite, sid)
        assert row["transcript_path"].startswith(str(backed_up.repo.path))
        expected = _backup_sha(backed_up.repo.path, backed_up.store.sqlite, sid)
        again = export_session(fresh.sqlite, row, backed_up.tmp / "re-export")
        assert again.files.sha256 == expected
        assert BackupState.load(fresh.data_dir).sessions[sid].sha256 == expected
    assert not (fresh.data_dir / "backup-restore").exists()


def test_restore_second_run_skips_everything(backed_up: SimpleNamespace) -> None:
    fresh = _fresh_store(backed_up.tmp, "restored")
    restore_backup(fresh, backed_up.repo.path)

    report = restore_backup(fresh, backed_up.repo.path)

    assert (report.imported, report.skipped, report.errors) == (0, 2, [])


def test_restore_then_backup_from_restored_store_changes_nothing(
    backed_up: SimpleNamespace,
) -> None:
    fresh = _fresh_store(backed_up.tmp, "restored")
    restore_backup(fresh, backed_up.repo.path)
    clone_b = GitRepo.clone(str(backed_up.bare), backed_up.tmp / "clone-b")
    BackupConfig(enabled=True, remote=str(backed_up.bare), path=str(clone_b.path)).save(
        fresh.data_dir
    )
    head_before = _bare_head(backed_up.bare)

    report = run_backup(fresh.sqlite, fresh.data_dir)

    assert (report.planned, report.commits) == (0, 0)
    assert _bare_head(backed_up.bare) == head_before


def test_restore_handles_split_parts(
    temp_store: Any,
    tmp_path: Path,
    bare_remote: Path,
    sample_session_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    _ingest(temp_store, sample_session_file)
    repo = GitRepo.init(tmp_path / "backup", str(bare_remote))
    BackupConfig(enabled=True, remote=str(bare_remote), path=str(repo.path)).save(
        temp_store.data_dir
    )
    run_backup(temp_store.sqlite, temp_store.data_dir, part_bytes=200)
    meta = read_session_json(
        repo.path / Path(session_rel_dir(temp_store.sqlite.get_session("test-session-1")))
    )
    assert len(meta["parts"]) > 1

    fresh = _fresh_store(tmp_path, "restored")
    report = restore_backup(fresh, repo.path)

    assert (report.imported, report.errors) == (1, [])
    assert _event_count(fresh.sqlite, "test-session-1") == _event_count(
        temp_store.sqlite, "test-session-1"
    )
    assert fresh.sqlite.get_session("test-session-1")["transcript_path"].endswith(meta["parts"][0])


def test_restore_codex_session(
    temp_store: Any, tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(CODEX_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    _ingest(temp_store, rollout)
    with temp_store.sqlite.connect() as conn:
        codex_ids = [
            r[0]
            for r in conn.execute("SELECT session_id FROM sessions WHERE session_id LIKE 'codex:%'")
        ]
    assert codex_ids == ["codex:shape-codex-1"]
    repo = GitRepo.init(tmp_path / "backup", str(bare_remote))
    BackupConfig(enabled=True, remote=str(bare_remote), path=str(repo.path)).save(
        temp_store.data_dir
    )
    run_backup(temp_store.sqlite, temp_store.data_dir)

    fresh = _fresh_store(tmp_path, "restored")
    report = restore_backup(fresh, repo.path)

    assert (report.imported, report.errors) == (1, [])
    assert fresh.sqlite.get_session("codex:shape-codex-1") is not None
    assert _event_count(fresh.sqlite, "codex:shape-codex-1") == _event_count(
        temp_store.sqlite, "codex:shape-codex-1"
    )


# ─── Config, errors, format ───────────────────────────────────────────────────


def test_restore_copies_config_only_when_absent(backed_up: SimpleNamespace) -> None:
    fresh = _fresh_store(backed_up.tmp, "restored")
    report = restore_backup(fresh, backed_up.repo.path)
    assert report.config_copied is True
    copied = json.loads((fresh.data_dir / "config.json").read_text())
    assert copied["hook"] == {"enabled": True}
    assert "backup" not in copied  # the old machine's clone path and switch do not carry over

    other = _fresh_store(backed_up.tmp, "other")
    (other.data_dir / "config.json").write_text(json.dumps({"hook": {"max_episodes": 9}}))
    report = restore_backup(other, backed_up.repo.path)
    assert report.config_copied is False
    assert json.loads((other.data_dir / "config.json").read_text()) == {"hook": {"max_episodes": 9}}


def test_copy_config_if_absent_without_a_repo_config(tmp_path: Path) -> None:
    (tmp_path / "repo").mkdir()
    (tmp_path / "data").mkdir()
    assert copy_config_if_absent(tmp_path / "repo", tmp_path / "data") is False
    assert not (tmp_path / "data" / "config.json").exists()


def test_restore_records_a_broken_session_and_keeps_going(backed_up: SimpleNamespace) -> None:
    broken_dir = backed_up.repo.path / Path(
        session_rel_dir(backed_up.store.sqlite.get_session("edit-session"))
    )
    (broken_dir / "session.json").write_text("{not json")
    fresh = _fresh_store(backed_up.tmp, "restored")

    report = restore_backup(fresh, backed_up.repo.path)

    assert (report.imported, report.skipped) == (1, 0)
    assert len(report.errors) == 1
    assert "edit-session" in report.errors[0]["session"]
    assert fresh.sqlite.get_session("test-session-1") is not None
    assert fresh.sqlite.get_session("edit-session") is None


def test_restore_refuses_newer_format(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "sessions").mkdir(parents=True)
    write_manifest(repo, 0)
    manifest = json.loads((repo / "manifest.json").read_text())
    manifest["format"] = 2
    (repo / "manifest.json").write_text(json.dumps(manifest))
    fresh = _fresh_store(tmp_path, "restored")

    with pytest.raises(ValueError):
        restore_backup(fresh, repo)
    assert not (fresh.data_dir / "backup-state.json").exists()


def test_restore_refuses_a_directory_that_is_not_a_backup(tmp_path: Path) -> None:
    (tmp_path / "nothing").mkdir()
    fresh = _fresh_store(tmp_path, "restored")
    with pytest.raises(ValueError):
        restore_backup(fresh, tmp_path / "nothing")


def test_iter_backup_sessions_sorted_and_tolerant(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    b = repo / "sessions" / "claude" / "2026" / "02" / "b"
    a = repo / "sessions" / "claude" / "2026" / "01" / "a"
    b.mkdir(parents=True)
    a.mkdir(parents=True)
    (a / "session.json").write_text(json.dumps({"session_id": "a", "parts": ["transcript.jsonl"]}))
    (b / "session.json").write_text("garbage")

    found = list(iter_backup_sessions(repo))

    assert [p for p, _ in found] == [a, b]
    assert found[0][1]["session_id"] == "a"
    assert found[1][1] == {}


def test_materialize_transcript_single_and_parts(tmp_path: Path) -> None:
    session_dir = tmp_path / "s"
    session_dir.mkdir()
    (session_dir / "transcript.jsonl").write_text('{"a":1}\n')
    single = materialize_transcript(
        session_dir, {"session_id": "s", "parts": ["transcript.jsonl"]}, tmp_path / "work"
    )
    assert single == session_dir / "transcript.jsonl"

    (session_dir / "transcript.part-0001.jsonl").write_bytes(b'{"a":1}\n')
    (session_dir / "transcript.part-0002.jsonl").write_bytes(b'{"b":2}\n')
    meta = {
        "session_id": "codex:x/y",
        "parts": ["transcript.part-0001.jsonl", "transcript.part-0002.jsonl"],
    }
    joined = materialize_transcript(session_dir, meta, tmp_path / "work")
    assert joined.parent == tmp_path / "work"
    assert joined.read_bytes() == b'{"a":1}\n{"b":2}\n'
    shutil.rmtree(tmp_path / "work")


# ─── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_restore_cli_from_remote_and_hint(
    runner: CliRunner, backed_up: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(backed_up.tmp))
    fresh_dir = backed_up.tmp / "fresh"

    result = runner.invoke(
        app,
        ["restore", "--from", str(backed_up.bare), "--data-dir", str(fresh_dir), "--skip-analysis"],
    )

    assert result.exit_code == 0, result.output
    assert "2 session(s) imported" in result.output
    assert f"longhand backup init --remote {backed_up.bare}" in result.output
    assert (fresh_dir / "backup" / ".git").exists()
    fresh = _fresh_store(backed_up.tmp, "fresh")
    for sid in backed_up.ids:
        assert fresh.sqlite.get_session(sid) is not None

    again = runner.invoke(app, ["restore", "--data-dir", str(fresh_dir), "--skip-analysis"])
    assert again.exit_code == 0, again.output
    assert "2 already present" in again.output


def test_restore_cli_from_a_local_clone(runner: CliRunner, backed_up: SimpleNamespace) -> None:
    fresh_dir = backed_up.tmp / "fresh"

    result = runner.invoke(
        app,
        [
            "restore",
            "--from",
            str(backed_up.repo.path),
            "--data-dir",
            str(fresh_dir),
            "--skip-analysis",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "2 session(s) imported" in result.output
    assert not (fresh_dir / "backup").exists()  # used in place, no clone made


def test_restore_cli_without_a_repo_exits_one(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(app, ["restore", "--data-dir", str(tmp_path / "empty")])
    assert result.exit_code == 1, result.output
    assert "--from" in result.output

    bad = runner.invoke(
        app, ["restore", "--from", "ext::sh -c id", "--data-dir", str(tmp_path / "empty")]
    )
    assert bad.exit_code == 1, bad.output
