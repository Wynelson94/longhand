"""Tests for the storage layer."""

from __future__ import annotations

from longhand.parser import JSONLParser


def test_ingest_and_query_roundtrip(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    result = temp_store.ingest_session(session, events)
    assert result["events_stored"] == len(events)

    # Session retrievable
    stored = temp_store.sqlite.get_session(session.session_id)
    assert stored is not None
    assert stored["session_id"] == "test-session-1"

    # Events retrievable
    stored_events = temp_store.sqlite.get_events(session_id=session.session_id)
    assert len(stored_events) == len(events)


def test_file_edits_filter(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    edits = temp_store.sqlite.get_file_edits("/tmp/test-project/README.md")
    assert len(edits) == 1
    assert edits[0]["tool_name"] == "Edit"


def test_stats(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    stats = temp_store.sqlite.get_stats()
    assert stats["sessions"] == 1
    assert stats["events"] == len(events)
    assert stats["tool_calls"] >= 2
    assert stats["thinking_blocks"] >= 1


def test_already_ingested_detection(sample_session_file, temp_store):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    assert not temp_store.sqlite.already_ingested(
        str(sample_session_file), sample_session_file.stat().st_size
    )

    temp_store.ingest_session(session, events)

    assert temp_store.sqlite.already_ingested(
        str(sample_session_file), sample_session_file.stat().st_size
    )


def test_skip_analysis_populates_sqlite_without_vectors(
    sample_session_file, temp_store
):
    """`run_analysis=False` populates SQLite but leaves episode/segment vectors empty.

    This is the fast path powered by the CLI `--skip-analysis` flag: users
    with a large backfill get a working SQLite store immediately and can
    fill in semantic recall later via `longhand reanalyze`.
    """
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    result = temp_store.ingest_session(session, events, run_analysis=False)

    # SQLite side — events and session are present, analysis outputs are not.
    assert result["events_stored"] == len(events)
    assert result["episodes"] == 0
    assert "segments" not in result
    assert temp_store.sqlite.get_session(session.session_id) is not None

    # Vector side — episode + segment collections stay empty (analysis is
    # where those get populated). The events collection, which is populated
    # in the pre-analysis phase, is allowed to be non-empty.
    assert temp_store.vectors.episode_count() == 0
    assert temp_store.vectors.segment_count() == 0


def test_batched_embeddings_match_per_item(tmp_path):
    """Batched episode+segment embedding path produces the same IDs as per-item.

    Guards against regressions in `add_episode_embeddings_batch` and
    `add_segment_embeddings_batch`. Two fresh stores: one analyzed via the
    current (batched) code path, one via the legacy per-item path. The IDs
    materialized in each collection must match exactly.
    """
    from longhand.storage import LonghandStore
    from longhand.storage.store import _build_episode_text

    # Build a realistic-enough in-memory session by reusing multi_edit fixture
    # inline — keeps the test self-contained.
    import json

    session_path = tmp_path / "batch-test.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "batch-s",
            "timestamp": "2026-04-20T10:00:00.000Z",
            "cwd": "/tmp/proj",
            "gitBranch": "main",
            "isSidechain": False,
            "message": {"role": "user", "content": "Fix the bug in main.py"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "batch-s",
            "timestamp": "2026-04-20T10:00:01.000Z",
            "cwd": "/tmp/proj",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Looking at main.py now."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/proj/main.py",
                            "old_string": "buggy",
                            "new_string": "fixed",
                            "replace_all": False,
                        },
                    },
                ],
            },
        },
        {
            "type": "user",
            "uuid": "r1",
            "parentUuid": "a1",
            "sessionId": "batch-s",
            "timestamp": "2026-04-20T10:00:02.000Z",
            "cwd": "/tmp/proj",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "OK"}],
            },
            "toolUseResult": {"success": True},
        },
    ]
    with session_path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    parser = JSONLParser(session_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    # Batched path (current production code).
    batched = LonghandStore(data_dir=tmp_path / "batched")
    batched.ingest_session(session, events, run_analysis=True)
    batched_episode_ids = set(batched.vectors.episodes_collection.get()["ids"])
    batched_segment_ids = set(batched.vectors.segments_collection.get()["ids"])

    # Per-item path: re-run analysis but call the singular methods directly
    # to mirror pre-v0.5.12 behavior.
    per_item = LonghandStore(data_dir=tmp_path / "per-item")
    per_item.ingest_session(session, events, run_analysis=False)
    per_item.sqlite.upsert_session(session)

    from longhand.analysis.episode_extraction import extract_episodes
    from longhand.analysis.project_inference import infer_project
    from longhand.analysis.segment_extraction import extract_segments

    project = infer_project(session, events)
    per_item.sqlite.upsert_project(project)
    per_item.sqlite.attach_session_to_project(session.session_id, project["project_id"])

    episodes = extract_episodes(
        session_id=session.session_id,
        project_id=project["project_id"],
        events=events,
    )
    per_item.sqlite.insert_episodes(episodes)
    for ep in episodes:
        if not ep.get("fix_event_id"):
            continue
        text = _build_episode_text(ep)
        if not text:
            continue
        per_item.vectors.add_episode_embedding(
            episode_id=ep["episode_id"],
            text=text,
            metadata={
                "session_id": session.session_id,
                "project_id": project["project_id"] or "",
                "ended_at": ep["ended_at"],
                "status": ep.get("status", "unresolved"),
                "has_fix": True,
            },
        )

    segments = extract_segments(
        session_id=session.session_id,
        project_id=project["project_id"],
        events=events,
    )
    per_item.sqlite.insert_segments(segments)
    for seg in segments:
        per_item.vectors.add_segment_embedding(
            segment_id=seg["segment_id"],
            text=seg["summary"],
            metadata={
                "session_id": session.session_id,
                "project_id": project["project_id"] or "",
                "segment_type": seg.get("segment_type", "discussion"),
                "started_at": seg["started_at"],
                "ended_at": seg["ended_at"],
            },
        )

    per_item_episode_ids = set(per_item.vectors.episodes_collection.get()["ids"])
    per_item_segment_ids = set(per_item.vectors.segments_collection.get()["ids"])

    assert batched_episode_ids == per_item_episode_ids
    assert batched_segment_ids == per_item_segment_ids
