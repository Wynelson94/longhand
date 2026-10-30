"""Tests for the backup service: config, sizing, the run itself, auto mode, status.

The run is exercised end to end against a bare repository under ``tmp_path``:
export → reconcile → commit → push, then a fresh clone proves what the remote
holds. Masking is on by default, so the token in the fixture session must be
masked in the clone while the local store keeps it verbatim.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from longhand.backup import service
from longhand.backup.export import iter_session_lines, mask_line, read_manifest
from longhand.backup.gitrepo import GitRepo, git_executable
from longhand.backup.service import (
    STAGING_DIRNAME,
    BackupBusyError,
    BackupConfig,
    BackupDisabledError,
    RunReport,
    backup_status,
    backup_status_line,
    estimate_export,
    log_line,
    maybe_auto_backup,
    run_backup,
)
from longhand.backup.state import STATE_FILENAME, BackupState, session_marks
from longhand.parser import JSONLParser
from longhand.timeutil import utcnow
from longhand.types import Event, EventType

TOKEN = "ghp_" + "A" * 36
MASKED = "ghp_…AAAA (len=40)"

pytestmark = [
    pytest.mark.skipif(git_executable() is None, reason="git is not installed"),
    pytest.mark.usefixtures("isolated_git_config"),
]


def _ingest(store: Any, path: Path) -> None:
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)


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


def _bare_subjects(bare: Path) -> list[str]:
    """Commit subjects on the bare remote's main, newest first; [] when it has none."""
    git = git_executable()
    assert git is not None
    proc = subprocess.run(
        [git, "--git-dir", str(bare), "log", "--format=%s", "main"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.splitlines() if proc.returncode == 0 else []


@pytest.fixture
def env(temp_store: Any, tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    repo = GitRepo.init(tmp_path / "backup", str(bare_remote))
    BackupConfig(enabled=True, remote=str(bare_remote), path=str(repo.path)).save(
        temp_store.data_dir
    )
    return SimpleNamespace(
        store=temp_store,
        sqlite=temp_store.sqlite,
        data_dir=temp_store.data_dir,
        bare=bare_remote,
        repo=repo,
        tmp=tmp_path,
    )


def _transcript(root: Path, session_id: str) -> Path:
    return root / "sessions" / "claude" / "2026" / "05" / session_id / "transcript.jsonl"


# ─── Refusals ─────────────────────────────────────────────────────────────────


def test_run_backup_refuses_when_disabled(temp_store: Any) -> None:
    with pytest.raises(BackupDisabledError):
        run_backup(temp_store.sqlite, temp_store.data_dir)


def test_run_backup_refuses_without_a_repo(temp_store: Any, tmp_path: Path) -> None:
    BackupConfig(enabled=True, remote="/nowhere.git", path=str(tmp_path / "missing")).save(
        temp_store.data_dir
    )
    with pytest.raises(BackupDisabledError):
        run_backup(temp_store.sqlite, temp_store.data_dir)


# ─── The run ──────────────────────────────────────────────────────────────────


def test_run_backup_first_run_commits_and_pushes(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", f"use {TOKEN} for the API call"))

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.planned, report.added, report.grown) == (1, 1, 0)
    assert (report.commits, report.pushed, report.error, report.dry_run) == (1, True, None, False)
    assert report.bytes_exported > 0
    assert report.mask_counts == {"github_token": 1}
    assert report.sessions_masked == 1
    assert _bare_subjects(env.bare) == ["longhand backup: 1 added, 0 updated"]

    clone = GitRepo.clone(str(env.bare), env.tmp / "clone")
    text = _transcript(clone.path, "s-one").read_text(encoding="utf-8")
    assert TOKEN not in text
    first = json.loads(text.splitlines()[0])
    assert first["message"]["content"] == f"use {MASKED} for the API call"
    assert (clone.path / "README.md").exists()
    assert (clone.path / "config.json").exists()
    manifest = read_manifest(clone.path)
    assert manifest is not None and manifest["sessions"] == 1

    state = BackupState.load(env.data_dir)
    assert state.sessions["s-one"].sha256
    assert state.last_push_at is not None
    assert state.last_run_at is not None
    assert state.last_error is None
    assert state.last_mask_counts == {"github_token": 1}
    assert state.last_run_summary["added"] == 1
    assert not (env.repo.path / STAGING_DIRNAME).exists()


def test_run_backup_second_run_is_noop(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    run_backup(env.sqlite, env.data_dir)
    head = env.repo.head()

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.planned, report.commits, report.pushed, report.error) == (0, 0, False, None)
    assert env.repo.head() == head
    assert len(_bare_subjects(env.bare)) == 1


def test_run_backup_exports_a_grown_session_as_updated(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    run_backup(env.sqlite, env.data_dir)

    late = Event(
        event_id="s-one-late-1",
        session_id="s-one",
        event_type=EventType.USER_MESSAGE,
        sequence=500,
        timestamp=datetime(2026, 5, 1, 9, 5, tzinfo=timezone.utc),
        content="one more",
        raw={"type": "user", "uuid": "s-one-late-1", "sessionId": "s-one"},
    )
    env.sqlite.insert_events([late])

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.planned, report.added, report.grown, report.commits) == (1, 0, 1, 1)
    assert _bare_subjects(env.bare)[0] == "longhand backup: 0 added, 1 updated"
    lines = _transcript(env.repo.path, "s-one").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["uuid"] == "s-one-late-1"


def test_run_backup_dry_run_changes_nothing(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", f"use {TOKEN} now"))

    report = run_backup(env.sqlite, env.data_dir, dry_run=True)

    assert report.dry_run is True
    assert report.planned == 1
    assert report.bytes_exported > 0
    assert report.mask_counts == {"github_token": 1}
    assert report.commits == 0
    assert env.repo.head() is None
    assert not (env.data_dir / STATE_FILENAME).exists()
    assert _bare_subjects(env.bare) == []


def test_run_backup_batches_pushes(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "one"))
    _ingest(env.store, _session_file(env.tmp, "s-two", "two"))

    report = run_backup(env.sqlite, env.data_dir, batch_bytes=1)

    assert report.added == 2
    assert report.commits == 2
    assert report.pushed is True
    assert len(_bare_subjects(env.bare)) == 2
    first_commit_files = env.repo.run("ls-tree", "-r", "--name-only", "HEAD~1").stdout.split()
    assert "README.md" in first_commit_files
    assert "manifest.json" in first_commit_files
    manifest = read_manifest(env.repo.path)
    assert manifest is not None and manifest["sessions"] == 2


def _point_at(env: Any, remote: Path) -> None:
    """Re-point both the clone and the config at ``remote`` (a dead one simulates a push failure)."""
    env.repo.run("remote", "set-url", "origin", "--", str(remote))
    BackupConfig(enabled=True, remote=str(remote), path=str(env.repo.path)).save(env.data_dir)


def test_run_backup_records_push_failure(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    _point_at(env, env.tmp / "gone.git")

    report = run_backup(env.sqlite, env.data_dir)

    assert report.error
    assert report.pushed is False
    assert report.commits == 1
    state = BackupState.load(env.data_dir)
    assert state.last_push_at is None
    assert state.last_error
    assert state.sessions["s-one"].sha256
    assert env.repo.unpushed_commits() == 1


def test_run_backup_pushes_what_an_earlier_run_left_behind(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    _point_at(env, env.tmp / "gone.git")
    run_backup(env.sqlite, env.data_dir)
    _point_at(env, env.bare)

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.planned, report.commits, report.pushed, report.error) == (0, 0, True, None)
    assert len(_bare_subjects(env.bare)) == 1
    state = BackupState.load(env.data_dir)
    assert state.last_push_at is not None
    assert state.last_error is None


def test_run_backup_commits_leftovers_from_an_interrupted_run(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    stray = env.repo.path / "sessions" / "stray.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("half-written\n")

    report = run_backup(env.sqlite, env.data_dir)

    assert report.commits == 2
    subjects = _bare_subjects(env.bare)
    assert subjects[-1] == "longhand backup: leftover from an interrupted run"
    clone = GitRepo.clone(str(env.bare), env.tmp / "clone")
    assert (clone.path / "sessions" / "stray.txt").read_text() == "half-written\n"


def test_run_backup_fast_forwards_when_another_machine_pushed(env: Any) -> None:
    other = GitRepo.init(env.tmp / "other", str(env.bare))
    other_dir = other.path / "sessions" / "claude" / "2026" / "01" / "other-1"
    other_dir.mkdir(parents=True)
    (other_dir / "session.json").write_text(json.dumps({"session_id": "other-1"}) + "\n")
    (other_dir / "transcript.jsonl").write_text('{"type":"user"}\n')
    other.add_all()
    other.commit("from another machine")
    other.push()
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.added, report.pushed, report.error) == (1, True, None)
    assert (
        env.repo.path / "sessions" / "claude" / "2026" / "01" / "other-1" / "transcript.jsonl"
    ).exists()
    clone = GitRepo.clone(str(env.bare), env.tmp / "clone")
    assert _transcript(clone.path, "s-one").exists()
    assert (
        clone.path / "sessions" / "claude" / "2026" / "01" / "other-1" / "session.json"
    ).exists()


def test_run_backup_no_push_commits_locally(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))

    report = run_backup(env.sqlite, env.data_dir, push=False)

    assert (report.commits, report.pushed, report.error) == (1, False, None)
    assert _bare_subjects(env.bare) == []
    state = BackupState.load(env.data_dir)
    assert state.last_push_at is None
    assert state.last_error is None


def test_run_backup_unmasked_exports_verbatim(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", f"use {TOKEN} now"))

    report = run_backup(env.sqlite, env.data_dir, mask=False, push=False)

    assert report.mask_counts == {}
    assert report.sessions_masked == 0
    text = _transcript(env.repo.path, "s-one").read_text(encoding="utf-8")
    assert TOKEN in text
    meta = json.loads((_transcript(env.repo.path, "s-one").parent / "session.json").read_text())
    assert meta["masked"] is False


def test_run_backup_reports_progress(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    seen: list[str] = []

    run_backup(env.sqlite, env.data_dir, progress=seen.append)

    assert any("s-one" in line for line in seen)


# ─── Auto mode ────────────────────────────────────────────────────────────────


def test_maybe_auto_backup_conditions(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    calls: list[dict[str, Any]] = []

    def fake_run(sqlite: Any, data_dir: Path, **kwargs: Any) -> RunReport:
        calls.append(kwargs)
        return RunReport(planned=1, added=1, commits=1, pushed=True)

    monkeypatch.setattr(service, "run_backup", fake_run)

    assert maybe_auto_backup(env.sqlite, env.data_dir) is None  # auto off
    assert calls == []

    BackupConfig(enabled=True, remote=str(env.bare), path=str(env.repo.path), auto=True).save(
        env.data_dir
    )
    assert maybe_auto_backup(env.sqlite, env.data_dir) is not None  # never pushed
    assert calls[-1]["mask"] is True and calls[-1]["push"] is True

    now = utcnow()
    state = BackupState(last_push_at=(now - timedelta(minutes=10)).isoformat())
    state.save(env.data_dir)
    assert maybe_auto_backup(env.sqlite, env.data_dir, now=now) is None  # too soon

    state.last_push_at = (now - timedelta(hours=2)).isoformat()
    state.save(env.data_dir)
    assert maybe_auto_backup(env.sqlite, env.data_dir, now=now) is not None

    state.sessions = session_marks(env.sqlite)
    state.save(env.data_dir)
    assert maybe_auto_backup(env.sqlite, env.data_dir, now=now) is None  # nothing changed
    assert len(calls) == 2


def test_maybe_auto_backup_swallows_errors_and_logs(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    BackupConfig(enabled=True, remote=str(env.bare), path=str(env.repo.path), auto=True).save(
        env.data_dir
    )

    def boom(sqlite: Any, data_dir: Path, **kwargs: Any) -> RunReport:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "run_backup", boom)

    assert maybe_auto_backup(env.sqlite, env.data_dir) is None
    log = (env.data_dir / "logs" / "backup.log").read_text(encoding="utf-8")
    assert "boom" in log


def test_maybe_auto_backup_runs_for_real_and_logs_the_summary(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    BackupConfig(enabled=True, remote=str(env.bare), path=str(env.repo.path), auto=True).save(
        env.data_dir
    )

    report = maybe_auto_backup(env.sqlite, env.data_dir)

    assert report is not None and report.pushed is True
    assert len(_bare_subjects(env.bare)) == 1
    log = (env.data_dir / "logs" / "backup.log").read_text(encoding="utf-8")
    assert "1 added" in log


# ─── Status ───────────────────────────────────────────────────────────────────


def test_backup_status_line_variants(tmp_path: Path) -> None:
    data_dir = tmp_path / "lh"
    data_dir.mkdir()
    assert backup_status_line(data_dir) == "off"

    BackupConfig(enabled=True, remote="/r.git").save(data_dir)
    assert backup_status_line(data_dir) == "on · never pushed"

    now = utcnow().isoformat()
    older = (utcnow() - timedelta(hours=3)).isoformat()
    BackupState(last_run_at=now, last_push_at=now).save(data_dir)
    assert backup_status_line(data_dir) == "on · last push just now"

    BackupState(last_run_at=now, last_push_at=None, last_error="push failed").save(data_dir)
    assert backup_status_line(data_dir) == "on · last run FAILED just now"

    BackupState(last_run_at=now, last_push_at=older, last_error="push failed").save(data_dir)
    assert backup_status_line(data_dir) == "on · last run FAILED just now"

    BackupConfig(enabled=True, remote="/r.git", auto=True).save(data_dir)
    BackupState(last_run_at=now, last_push_at=older).save(data_dir)
    assert backup_status_line(data_dir) == "auto · last push 3 h ago"


def test_backup_status_reports_pending_and_masks_the_remote(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    BackupConfig(
        enabled=True, remote="https://user:tok@github.com/o/r.git", path=str(env.repo.path)
    ).save(env.data_dir)

    status = backup_status(env.data_dir, env.sqlite)

    assert status["enabled"] is True and status["auto"] is False
    assert status["remote"] == "https://***@github.com/o/r.git"
    assert status["path"] == str(env.repo.path)
    assert status["repo_exists"] is True
    assert status["pending"] == 1
    assert status["sessions_backed_up"] == 0
    assert status["last_push_at"] is None and status["last_error"] is None
    assert status["unpushed_commits"] == 0

    BackupConfig(enabled=True, remote=str(env.bare), path=str(env.repo.path)).save(env.data_dir)
    run_backup(env.sqlite, env.data_dir)
    status = backup_status(env.data_dir, env.sqlite)
    assert status["pending"] == 0
    assert status["sessions_backed_up"] == 1
    assert status["repo_bytes"] > 0
    assert status["last_push_at"] is not None


def test_backup_status_when_disabled(temp_store: Any) -> None:
    status = backup_status(temp_store.data_dir, temp_store.sqlite)
    assert status["enabled"] is False
    assert status["repo_exists"] is False
    assert status["pending"] == 0
    assert status["remote"] is None


# ─── Config ───────────────────────────────────────────────────────────────────


def test_backup_config_round_trip_keeps_other_namespaces(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"hook": {"enabled": True}}))

    BackupConfig(enabled=True, remote="/r.git", path="/p", auto=True).save(tmp_path)

    data = json.loads((tmp_path / "config.json").read_text())
    assert data["hook"] == {"enabled": True}
    assert data["backup"] == {"enabled": True, "remote": "/r.git", "path": "/p", "auto": True}
    loaded = BackupConfig.load(tmp_path)
    assert loaded == BackupConfig(enabled=True, remote="/r.git", path="/p", auto=True)
    assert loaded.repo_path(tmp_path) == Path("/p")


def test_backup_config_load_tolerates_missing_or_odd_files(tmp_path: Path) -> None:
    assert BackupConfig.load(tmp_path) == BackupConfig()
    assert BackupConfig().repo_path(tmp_path) == tmp_path / "backup"

    (tmp_path / "config.json").write_text(json.dumps({"backup": "nope"}))
    assert BackupConfig.load(tmp_path) == BackupConfig()

    (tmp_path / "config.json").write_text(json.dumps({"backup": {"enabled": "yes", "auto": 1}}))
    assert BackupConfig.load(tmp_path) == BackupConfig()

    (tmp_path / "config.json").write_text("{not json")
    assert BackupConfig.load(tmp_path) == BackupConfig()
    with pytest.raises(ValueError):
        BackupConfig(enabled=True).save(tmp_path)


def test_backup_config_save_creates_the_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "fresh"
    BackupConfig(enabled=True, remote="/r.git").save(data_dir)
    assert json.loads((data_dir / "config.json").read_text())["backup"]["remote"] == "/r.git"


# ─── Sizing ───────────────────────────────────────────────────────────────────


def test_estimate_export_counts_sessions_bytes_and_masks(
    temp_store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    _ingest(temp_store, _session_file(tmp_path, "s-one", f"use {TOKEN} for the API call"))
    raw_lines = list(iter_session_lines(temp_store.sqlite, "s-one"))
    masked_lines = [mask_line(raw)[0] for raw in raw_lines]

    est = estimate_export(temp_store.sqlite)

    assert (est.sessions, est.claude, est.codex) == (1, 1, 0)
    assert est.lines == len(masked_lines) == 2
    assert est.bytes == sum(len(line.encode("utf-8")) + 1 for line in masked_lines)
    assert est.mask_counts == {"github_token": 1}
    assert est.sessions_with_masks == 1

    verbatim = estimate_export(temp_store.sqlite, mask=False)
    assert verbatim.mask_counts == {}
    assert verbatim.bytes == sum(len(line.encode("utf-8")) + 1 for line in raw_lines)

    seen: list[tuple[int, int]] = []
    estimate_export(temp_store.sqlite, ["s-one"], progress=lambda i, n: seen.append((i, n)))
    assert seen[-1] == (1, 1)
    assert estimate_export(temp_store.sqlite, []).sessions == 0


# ─── Log ──────────────────────────────────────────────────────────────────────


def test_log_line_appends_and_never_raises(tmp_path: Path) -> None:
    log_line(tmp_path, "hello")
    log_line(tmp_path, "again")
    lines = (tmp_path / "logs" / "backup.log").read_text(encoding="utf-8").splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["hello", "again"]
    datetime.fromisoformat(lines[0].split(" ", 1)[0])

    blocker = tmp_path / "blocked"
    blocker.write_text("")
    log_line(blocker, "nowhere to write")  # data_dir is a file: swallowed
    shutil.rmtree(tmp_path / "logs")


def test_run_backup_copies_config_without_the_backup_namespace(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    backup_ns = {"enabled": True, "remote": str(env.bare), "path": str(env.repo.path)}
    (env.data_dir / "config.json").write_text(
        json.dumps({"hook": {"enabled": True}, "backup": {**backup_ns, "auto": False}})
    )

    run_backup(env.sqlite, env.data_dir, push=False)

    copied = json.loads((env.repo.path / "config.json").read_text())
    assert copied == {"hook": {"enabled": True}}

    # Same settings, a different backup namespace and key order: nothing to commit.
    (env.data_dir / "config.json").write_text(
        json.dumps({"backup": {**backup_ns, "auto": True}, "hook": {"enabled": True}})
    )
    report = run_backup(env.sqlite, env.data_dir, push=False)
    assert report.commits == 0


def test_run_backup_masks_a_token_that_follows_a_newline(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", f"first line\n{TOKEN}\nlast line"))

    report = run_backup(env.sqlite, env.data_dir, push=False)

    assert report.mask_counts == {"github_token": 1}
    text = _transcript(env.repo.path, "s-one").read_text(encoding="utf-8")
    assert TOKEN not in text
    assert json.loads(text.splitlines()[0])["message"]["content"] == (
        f"first line\n{MASKED}\nlast line"
    )


# ─── Review fixes: repo identity, divergence, durability, lock ────────────────


def test_run_backup_re_exports_sessions_missing_from_a_recreated_clone(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    run_backup(env.sqlite, env.data_dir, push=False)  # committed locally, never pushed
    shutil.rmtree(env.repo.path)
    GitRepo.init(env.repo.path, str(env.bare))

    assert backup_status(env.data_dir, env.sqlite)["pending"] == 1
    report = run_backup(env.sqlite, env.data_dir)

    assert (report.planned, report.added, report.pushed) == (1, 1, True)
    assert _bare_subjects(env.bare) == ["longhand backup: 1 added, 0 updated"]
    clone = GitRepo.clone(str(env.bare), env.tmp / "clone")
    assert _transcript(clone.path, "s-one").exists()


def test_run_backup_recovers_when_both_machines_wrote_the_manifest(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    run_backup(env.sqlite, env.data_dir, push=False)  # machine A, offline

    other = GitRepo.init(env.tmp / "other", str(env.bare))  # machine B, online
    other_dir = other.path / "sessions" / "claude" / "2026" / "01" / "other-1"
    other_dir.mkdir(parents=True)
    (other_dir / "session.json").write_text(json.dumps({"session_id": "other-1"}) + "\n")
    (other_dir / "transcript.jsonl").write_text('{"type":"user"}\n')
    (other.path / "manifest.json").write_text(json.dumps({"format": 1, "sessions": 1}) + "\n")
    other.add_all()
    other.commit("from another machine")
    other.push()

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.pushed, report.error) == (True, None)
    clone = GitRepo.clone(str(env.bare), env.tmp / "clone")
    assert _transcript(clone.path, "s-one").exists()
    assert (
        clone.path / "sessions" / "claude" / "2026" / "01" / "other-1" / "transcript.jsonl"
    ).exists()
    manifest = read_manifest(clone.path)
    assert manifest is not None and manifest["sessions"] == 2

    again = run_backup(env.sqlite, env.data_dir)
    assert (again.planned, again.commits) == (0, 0)


def test_run_backup_grows_a_session_where_the_repo_keeps_it(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "hello"))
    run_backup(env.sqlite, env.data_dir, push=False)
    moved = "sessions/claude/undated/s-one"
    (env.repo.path / "sessions" / "claude" / "undated").mkdir()
    env.repo.run("mv", "sessions/claude/2026/05/s-one", moved)
    env.repo.run("commit", "-q", "-m", "an older layout")

    late = Event(
        event_id="s-one-late-1",
        session_id="s-one",
        event_type=EventType.USER_MESSAGE,
        sequence=500,
        timestamp=datetime(2026, 5, 1, 9, 5, tzinfo=timezone.utc),
        content="one more",
        raw={"type": "user", "uuid": "s-one-late-1", "sessionId": "s-one"},
    )
    env.sqlite.insert_events([late])

    report = run_backup(env.sqlite, env.data_dir, push=False)

    assert (report.added, report.grown) == (0, 1)
    assert not (env.repo.path / "sessions" / "claude" / "2026" / "05" / "s-one").exists()
    lines = (env.repo.path / moved / "transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads((env.repo.path / moved / "session.json").read_text())["rel_dir"] == moved


def test_run_backup_records_a_failing_session_and_continues(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "one"))
    _ingest(env.store, _session_file(env.tmp, "s-two", "two"))
    real_export = service.export_session

    def flaky(sqlite: Any, row: dict[str, Any], dest: Path, **kwargs: Any) -> Any:
        if row["session_id"] == "s-one":
            raise OSError("disk says no")
        return real_export(sqlite, row, dest, **kwargs)

    monkeypatch.setattr(service, "export_session", flaky)

    report = run_backup(env.sqlite, env.data_dir)

    assert (report.added, report.failed) == (1, 1)
    assert report.error is not None and "s-one" in report.error and "disk says no" in report.error
    assert report.pushed is True
    state = BackupState.load(env.data_dir)
    assert state.last_error is not None and "s-one" in state.last_error
    assert "s-one" not in state.sessions
    assert backup_status_line(env.data_dir) == "on · last run FAILED just now"

    monkeypatch.setattr(service, "export_session", real_export)
    again = run_backup(env.sqlite, env.data_dir)
    assert (again.added, again.failed, again.error) == (1, 0, None)
    assert backup_status_line(env.data_dir) == "on · last push just now"


def test_run_backup_records_an_unexpected_exception_in_the_state(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "one"))

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("manifest exploded")

    monkeypatch.setattr(service, "write_manifest", boom)

    with pytest.raises(RuntimeError):
        run_backup(env.sqlite, env.data_dir)

    state = BackupState.load(env.data_dir)
    assert state.last_error is not None and "manifest exploded" in state.last_error
    assert backup_status_line(env.data_dir) == "on · last run FAILED just now"


def test_run_backup_cleans_sidecars_before_committing_leftovers(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "one"))
    run_backup(env.sqlite, env.data_dir, push=False)
    live = env.repo.path / "sessions" / "claude" / "2026" / "05" / "s-one"
    stale_merge = live.with_name(live.name + ".merge")
    stale_merge.mkdir()
    (stale_merge / "transcript.jsonl").write_text("partial\n")

    report = run_backup(env.sqlite, env.data_dir, push=False)

    assert report.commits == 0
    assert not stale_merge.exists()
    assert ".merge" not in env.repo.run("ls-tree", "-r", "--name-only", "HEAD").stdout


def test_run_backup_refuses_while_another_run_holds_the_lock(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "one"))
    (env.data_dir / "backup.lock").write_text(f"{os.getpid()}\n")

    with pytest.raises(BackupBusyError):
        run_backup(env.sqlite, env.data_dir)
    assert _bare_subjects(env.bare) == []


def test_run_backup_breaks_a_stale_lock(env: Any) -> None:
    _ingest(env.store, _session_file(env.tmp, "s-one", "one"))
    (env.data_dir / "backup.lock").write_text("999999999\n")

    report = run_backup(env.sqlite, env.data_dir)

    assert report.added == 1
    assert not (env.data_dir / "backup.lock").exists()
