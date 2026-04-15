"""Tests for project-inference fallback on match miss."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from longhand.parser import JSONLParser
from longhand.recall import project_fallback
from longhand.recall.project_match import match_projects


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
    with patch(
        "longhand.recall.project_fallback.trigger_background_ingest"
    ) as mock_trigger:
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
    with patch(
        "longhand.recall.project_fallback.discover_sessions", return_value=[]
    ):
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
