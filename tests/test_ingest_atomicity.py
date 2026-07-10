"""Ingest atomicity: the analysis_stage marker and reconcile's
partially_indexed bucket.

The ingest pipeline commits each step separately; before v0.12 a crash after
the session row landed left the session looking fully indexed to reconcile
while analysis and vectors were silently missing. The stage marker makes the
crash visible ('pending'), reconcile reports it, and --fix repairs it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longhand.parser import JSONLParser
from longhand.recall import reconcile as reconcile_mod
from longhand.recall.reconcile import run_reconcile


def _parse(fixture_path: Path):
    parser = JSONLParser(fixture_path)
    events = list(parser.parse_events())
    return parser.build_session(events), events


def _stage(store, transcript_path: str) -> str | None:
    return store.sqlite.analysis_stages().get(transcript_path)


def test_full_ingest_stamps_analyzed(sample_session_file, temp_store):
    session, events = _parse(sample_session_file)
    temp_store.ingest_session(session, events, run_analysis=True)
    assert _stage(temp_store, str(sample_session_file)) == "analyzed"


def test_skip_analysis_stamps_events(sample_session_file, temp_store):
    session, events = _parse(sample_session_file)
    temp_store.ingest_session(session, events, run_analysis=False)
    assert _stage(temp_store, str(sample_session_file)) == "events"


def test_crash_mid_pipeline_leaves_pending(sample_session_file, temp_store, monkeypatch):
    session, events = _parse(sample_session_file)

    def _boom(*args, **kwargs):
        raise RuntimeError("chroma exploded mid-ingest")

    monkeypatch.setattr(temp_store.vectors, "add_events", _boom)
    with pytest.raises(RuntimeError):
        temp_store.ingest_session(session, events, run_analysis=True)

    assert _stage(temp_store, str(sample_session_file)) == "pending"


def test_reconcile_flags_pending_as_partial_and_fix_repairs(
    sample_session_file, temp_store, monkeypatch
):
    """A crash *after* project attribution is the blind spot this PR closes:
    the session row has a project_id, so pre-v0.12 reconcile called it fully
    indexed while episodes/segments/vectors were silently missing. (Crashes
    *before* attribution leave project_id NULL and were always caught by the
    null_project bucket.)"""
    session, events = _parse(sample_session_file)

    # Crash the first ingest mid-analysis (after attribute_session_project).
    def _boom(*args, **kwargs):
        raise RuntimeError("chroma exploded mid-analysis")

    monkeypatch.setattr(temp_store.vectors, "add_episode_embeddings_batch", _boom)
    with pytest.raises(RuntimeError):
        temp_store.ingest_session(session, events, run_analysis=True)
    monkeypatch.undo()

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [sample_session_file])

    report = run_reconcile(temp_store, fix=False)
    assert report.partially_indexed == [str(sample_session_file)]
    assert report.fully_indexed == 0

    # --fix re-ingests the partial; the repaired session is fully indexed.
    fixed = run_reconcile(temp_store, fix=True)
    assert fixed.ingested == 1
    assert _stage(temp_store, str(sample_session_file)) == "analyzed"

    after = run_reconcile(temp_store, fix=False)
    assert after.partially_indexed == []
    assert after.fully_indexed == 1


def test_reconcile_tolerates_null_stage(sample_session_file, temp_store, monkeypatch):
    """Pre-v0.12 rows have analysis_stage NULL — they must classify as fully
    indexed, not partial (no re-analysis stampede on upgrade)."""
    session, events = _parse(sample_session_file)
    temp_store.ingest_session(session, events, run_analysis=True)

    with temp_store.sqlite.connect() as conn:
        conn.execute(
            "UPDATE ingestion_log SET analysis_stage = NULL WHERE transcript_path = ?",
            (str(sample_session_file),),
        )

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [sample_session_file])
    report = run_reconcile(temp_store, fix=False)
    assert report.partially_indexed == []
    assert report.fully_indexed == 1


def test_reconcile_leaves_deliberate_skip_analysis_alone(
    sample_session_file, temp_store, monkeypatch
):
    """'events' is an intentional --skip-analysis defer — the new partial
    bucket must not claim it as a crash. (Skip-analysis sessions have NULL
    project_id, so they stay in the pre-existing null_project bucket exactly
    as before this change.)"""
    session, events = _parse(sample_session_file)
    temp_store.ingest_session(session, events, run_analysis=False)
    assert _stage(temp_store, str(sample_session_file)) == "events"

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [sample_session_file])
    report = run_reconcile(temp_store, fix=False)
    assert report.partially_indexed == []
    assert report.null_project == [str(sample_session_file)]


def test_analyze_all_path_clears_events_stage(sample_session_file, temp_store):
    """analyze_session (the `analyze --all` entry point) stamps 'analyzed'."""
    session, events = _parse(sample_session_file)
    temp_store.ingest_session(session, events, run_analysis=False)
    assert _stage(temp_store, str(sample_session_file)) == "events"

    temp_store.analyze_session(session, events)
    assert _stage(temp_store, str(sample_session_file)) == "analyzed"


def test_full_ingest_advances_live_offset(sample_session_file, temp_store):
    """A full ingest consumed the whole file, so the live-tail cursor must
    advance to file_size — resetting it to 0 made the next Stop hook
    re-parse the entire transcript for nothing."""
    session, events = _parse(sample_session_file)
    temp_store.ingest_session(session, events, run_analysis=False)

    size = sample_session_file.stat().st_size
    assert temp_store.sqlite.get_live_offset(str(sample_session_file)) == size
