"""Tests for project-inference fallback on match miss."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from longhand.parser import JSONLParser
from longhand.recall import project_fallback
from longhand.recall.project_match import match_projects
from longhand.setup_commands import ingest_single_session


def _ingest(fixture_path, store):
    parser = JSONLParser(fixture_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events)
    return session


def test_match_happy_path_no_fallback(sample_session_file, temp_store, tmp_path):
    """When a project is already indexed, match works without the fallback."""
    _ingest(sample_session_file, temp_store)

    # Query for 'test-project' which should hit the ingested session's cwd.
    with patch("longhand.recall.project_fallback.trigger_background_ingest") as mock_trigger:
        results = match_projects(temp_store, "test-project")
        # Fallback should NOT fire on a successful match.
        mock_trigger.assert_not_called()

    assert len(results) > 0
    assert "test-project" in results[0].canonical_path.lower()


def test_match_miss_fallback_rebuilds_projects(sample_session_file, temp_store):
    """A session file that exists on disk but isn't yet ingested is discoverable via fallback."""
    # Simulate the real-world scenario: a JSONL exists under ~/.claude/projects
    # but Longhand hasn't ingested it yet. We prove the fallback runs cheap
    # project inference on that file and surfaces a match.
    with temp_store.sqlite.connect() as conn:
        pre_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert pre_count == 0, "temp_store should start with zero projects"

    with (
        patch(
            "longhand.recall.project_fallback.discover_sessions",
            return_value=[Path(sample_session_file)],
        ),
        patch(
            "longhand.recall.project_fallback.trigger_background_ingest",
            return_value=False,  # no real subprocess during tests
        ),
    ):
        results = match_projects(temp_store, "test-project")

    # The fallback should have inferred the project on the fly and matched it.
    assert len(results) > 0
    assert any("on-the-fly" in r.lower() for match in results for r in match.reasons)

    # And the project table should be populated by the cheap pass.
    with temp_store.sqlite.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        assert count >= 1


def test_infer_missing_projects_nothing_to_do(temp_store):
    """When everything on disk is already ingested, returns empty list."""
    with patch("longhand.recall.project_fallback.discover_sessions", return_value=[]):
        result = project_fallback.infer_missing_projects(temp_store)
    assert result == []


def test_trigger_background_ingest_skips_if_lock_held(temp_store):
    """If the lockfile is held by a live PID, skip spawning a new subprocess."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    # Write our own PID — which is definitely alive.
    lock.write_text(str(os.getpid()))

    with patch("subprocess.Popen") as mock_popen:
        started = project_fallback.trigger_background_ingest(temp_store)

    assert started is False
    mock_popen.assert_not_called()

    # Cleanup
    lock.unlink()


def test_trigger_background_ingest_cleans_stale_lock(temp_store):
    """If the lockfile holder is dead, we spawn a new ingest anyway."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    # PID 1 belongs to init / launchd — always alive on a real system,
    # so pick a PID that should not exist. PID 0 is treated as invalid.
    lock.write_text("0")

    with patch("subprocess.Popen") as mock_popen:
        started = project_fallback.trigger_background_ingest(temp_store)

    assert started is True
    mock_popen.assert_called_once()

    # Cleanup
    if lock.exists():
        lock.unlink()


def test_claim_and_release_lock(temp_store):
    """claim_ingest_lock writes our PID; release_ingest_lock removes it."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        lock.unlink()

    assert project_fallback.claim_ingest_lock(temp_store) is True
    assert lock.exists()
    assert lock.read_text().strip() == str(os.getpid())

    # Idempotent — calling again still returns True.
    assert project_fallback.claim_ingest_lock(temp_store) is True

    project_fallback.release_ingest_lock(temp_store)
    assert not lock.exists()


def test_claim_lock_blocked_by_other_alive_pid(temp_store):
    """If another alive PID owns the lock, claim fails."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)

    # Use the parent PID — should be alive during the test run.
    other_pid = os.getppid()
    lock.write_text(str(other_pid))

    assert project_fallback.claim_ingest_lock(temp_store) is False

    # Cleanup
    lock.unlink()


def test_fallback_recursion_guard(temp_store):
    """If the fallback re-infers but still no match, do not loop forever."""
    # Make infer_missing_projects return a fake fingerprint, but one that
    # won't actually match the query. The function should return [] without
    # blowing the stack.
    fake_fingerprint = {
        "project_id": "p_00000000deadbeef",
        "canonical_path": "/tmp/fake-project",
        "display_name": "fake project",
        "aliases": ["fake", "fake-project"],
        "keywords": [],
        "languages": [],
        "category": None,
        "first_seen": "2026-04-14T00:00:00+00:00",
        "last_seen": "2026-04-14T00:00:00+00:00",
        "new_edits": 0,
    }

    with (
        patch(
            "longhand.recall.project_fallback.infer_missing_projects",
            return_value=[fake_fingerprint],
        ),
        patch(
            "longhand.recall.project_fallback.trigger_background_ingest",
            return_value=False,
        ),
    ):
        # Query that won't match the fake project at all.
        results = match_projects(temp_store, "completely-unrelated-query")

    # Should return [] — and importantly, shouldn't have recursed infinitely.
    assert results == []


def test_claim_lock_reclaims_stale_pid(temp_store):
    """A lockfile left by a dead PID is removed and reclaimed."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    # PID 0 is treated as invalid/dead by _read_lock_pid/_lock_holder_alive.
    lock.write_text("0")

    assert project_fallback.claim_ingest_lock(temp_store) is True
    assert lock.read_text().strip() == str(os.getpid())

    project_fallback.release_ingest_lock(temp_store)
    assert not lock.exists()


def test_claim_lock_atomic_create_loses_race(temp_store):
    """If another process creates the lockfile between the exists() check and
    the create, O_CREAT|O_EXCL must lose cleanly instead of overwriting."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    other_pid = os.getppid()
    lock.write_text(str(other_pid))

    # Force the pre-check to say "no lockfile" so claim falls through to the
    # atomic create against a file that actually exists — simulating the race.
    with patch.object(Path, "exists", return_value=False):
        assert project_fallback.claim_ingest_lock(temp_store) is False

    # The racing winner's PID must be untouched.
    assert lock.read_text().strip() == str(other_pid)
    lock.unlink()


def test_trigger_background_ingest_spawn_target_is_executable(temp_store):
    """The spawned `-m` target must be an importable, runnable module.

    Regression guard: v0.11.1 spawned `-m longhand.cli` — a package with no
    __main__.py — so every background ingest died instantly and silently.
    """
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)

    with patch("subprocess.Popen") as mock_popen:
        assert project_fallback.trigger_background_ingest(temp_store) is True

    argv = mock_popen.call_args[0][0]
    assert argv[0] == sys.executable
    assert argv[1] == "-m"
    target = argv[2]
    spec = importlib.util.find_spec(target)
    assert spec is not None, f"spawn target {target!r} is not importable"
    if spec.submodule_search_locations is not None:
        assert importlib.util.find_spec(f"{target}.__main__") is not None, (
            f"spawn target {target!r} is a package without __main__.py — it dies instantly under -m"
        )


def test_python_dash_m_longhand_runs():
    """`python -m longhand` must keep working — the background spawn depends on it."""
    result = subprocess.run(
        [sys.executable, "-m", "longhand", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_ingest_single_session_defers_when_lock_held(sample_session_file, temp_store):
    """SessionEnd ingest defers with a success exit when another ingest owns the lock."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    # Another alive PID (claim is idempotent for our own PID, so use the parent).
    lock.write_text(str(os.getppid()))

    ingest_single_session(
        str(sample_session_file), data_dir=str(temp_store.data_dir), run_analysis=False
    )

    with temp_store.sqlite.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert n == 0  # deferred — nothing ingested, nothing raised
    assert lock.exists()  # not ours; must be left untouched
    lock.unlink()


def test_ingest_single_session_reclaims_stale_lock_and_releases(sample_session_file, temp_store):
    """A dead holder must not block SessionEnd; the lock is released afterwards."""
    lock = temp_store.data_dir / ".ingest.lock"
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    lock.write_text("0")  # PID 0 — treated as dead/invalid

    ingest_single_session(
        str(sample_session_file), data_dir=str(temp_store.data_dir), run_analysis=False
    )

    with temp_store.sqlite.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert n == 1
    assert not lock.exists()  # released in the finally


# ─── spawn_background generalization + episode-backfill trigger ──────────────


def _with_fix_episode(episode_id: str = "ep-bg") -> dict:
    return {
        "episode_id": episode_id,
        "session_id": "s-bg",
        "project_id": "p-bg",
        "started_at": "2026-07-01T10:00:00Z",
        "ended_at": "2026-07-01T10:30:00Z",
        "problem_event_id": f"{episode_id}-prob",
        "fix_event_id": f"{episode_id}-fix",
        "problem_description": "background backfill problem",
        "fix_summary": "background backfill fix",
        "touched_files": [],
        "tags": [],
        "status": "resolved",
    }


def test_trigger_background_episode_backfill_spawn_target(temp_store):
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)

    with patch("subprocess.Popen") as mock_popen:
        assert project_fallback.trigger_background_episode_backfill(temp_store) is True

    argv = mock_popen.call_args[0][0]
    assert argv == [sys.executable, "-m", "longhand", "backfill-episodes"]


def test_spawn_background_skips_when_lock_holder_alive(temp_store):
    temp_store.data_dir.mkdir(parents=True, exist_ok=True)
    lock = temp_store.data_dir / ".ingest.lock"
    lock.write_text(str(os.getppid()))  # an alive holder

    with patch("subprocess.Popen") as mock_popen:
        assert project_fallback.trigger_background_episode_backfill(temp_store) is False

    mock_popen.assert_not_called()
    lock.unlink()


def test_recall_spawns_backfill_instead_of_embedding_inline(temp_store, monkeypatch):
    """The recall pipeline runs inside the UserPromptSubmit hook — it must
    never embed the corpus inline. When a backfill is needed it spawns the
    detached worker and serves the current query from SQLite."""
    from longhand.recall import recall_pipeline

    temp_store.sqlite.insert_episodes([_with_fix_episode()])
    assert temp_store.episode_backfill_needed() is True

    spawned: list[int] = []
    monkeypatch.setattr(
        recall_pipeline,
        "trigger_background_episode_backfill",
        lambda store: spawned.append(1) or True,
    )

    def _no_inline(*args, **kwargs):
        raise AssertionError("recall embedded episodes inline")

    monkeypatch.setattr(temp_store, "backfill_episode_embeddings", _no_inline)
    monkeypatch.setattr(temp_store, "ensure_episode_embeddings", _no_inline)
    # Keep the match-miss fallback from scanning the real ~/.claude/projects.
    monkeypatch.setattr(recall_pipeline, "match_projects", lambda *a, **k: [])

    recall_pipeline.recall(temp_store, "background backfill problem")

    assert spawned == [1]
    assert temp_store.vectors.episode_count() == 0  # nothing embedded inline


def test_recall_does_not_spawn_when_vectors_populated(temp_store, monkeypatch):
    from longhand.recall import recall_pipeline

    temp_store.sqlite.insert_episodes([_with_fix_episode("ep-done")])
    temp_store.backfill_episode_embeddings()
    assert temp_store.episode_backfill_needed() is False

    spawned: list[int] = []
    monkeypatch.setattr(
        recall_pipeline,
        "trigger_background_episode_backfill",
        lambda store: spawned.append(1) or True,
    )
    monkeypatch.setattr(recall_pipeline, "match_projects", lambda *a, **k: [])

    recall_pipeline.recall(temp_store, "background backfill problem")

    assert spawned == []
