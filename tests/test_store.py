"""Tests for the storage layer."""

from __future__ import annotations

from datetime import datetime, timezone

from longhand.parser import JSONLParser


def test_iso_attaches_utc_to_naive_input():
    """_iso hardens naive datetimes to UTC so no writer can regress to
    local-clock stamps (v0.13 datetime normalization)."""
    from longhand.storage.sqlite_store import _iso

    assert _iso(datetime(2026, 7, 11, 12, 0)) == "2026-07-11T12:00:00+00:00"
    aware = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    assert _iso(aware) == "2026-07-11T12:00:00+00:00"


def test_ingest_timestamps_are_utc_aware(sample_session_file, temp_store):
    """sessions.ingested_at and ingestion_log.ingested_at carry a UTC offset.

    Legacy naive rows (pre-v0.13 local-clock stamps) stay readable — readers
    interpret naive values as UTC; no backfill migration.
    """
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events, run_analysis=False)

    with temp_store.sqlite.connect() as conn:
        session_stamp = conn.execute("SELECT ingested_at FROM sessions").fetchone()[0]
        log_stamp = conn.execute("SELECT ingested_at FROM ingestion_log").fetchone()[0]

    for stamp in (session_stamp, log_stamp):
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None, f"naive timestamp written: {stamp}"
        assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_resolve_data_dir_precedence(tmp_path, monkeypatch):
    """flag > LONGHAND_DATA_DIR env > ~/.longhand — the single resolution rule."""
    from longhand.storage.store import DEFAULT_DATA_DIR, resolve_data_dir

    monkeypatch.delenv("LONGHAND_DATA_DIR", raising=False)
    assert resolve_data_dir() == DEFAULT_DATA_DIR
    assert resolve_data_dir(tmp_path / "flag") == tmp_path / "flag"

    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "env"))
    assert resolve_data_dir() == tmp_path / "env"
    assert resolve_data_dir(tmp_path / "flag") == tmp_path / "flag"  # flag still wins


def test_longhand_store_honors_data_dir_env(tmp_path, monkeypatch):
    """Env inheritance is what carries a relocated store into hooks, spawned
    workers, and the MCP server without any of them taking a flag."""
    from longhand.storage import LonghandStore

    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "env-store"))
    store = LonghandStore()
    assert store.data_dir == tmp_path / "env-store"
    assert (tmp_path / "env-store" / "longhand.db").exists()


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


def test_project_counts_not_inflated_on_reingest(sample_session_file, temp_store):
    """Re-ingesting the same session must NOT inflate projects.session_count or
    total_edits.

    These columns reflect distinct attached sessions and the sum of their file
    edits, recomputed from the sessions table — not a per-ingest running tally.
    Regression: upsert_project() used to run `session_count = session_count + 1`
    (and `total_edits = total_edits + new_edits`) on every ingest, so a single
    re-ingested session inflated both counts. On the real corpus the home-dir
    project showed 2,068 "sessions" against 264 real ones.
    """
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    temp_store.ingest_session(session, events)
    project_id = temp_store.sqlite.get_session(session.session_id)["project_id"]
    p1 = temp_store.sqlite.get_project(project_id)
    assert p1["session_count"] == 1
    edits_after_first = p1["total_edits"]
    assert edits_after_first > 0  # the sample session has real file edits

    # Ingest the exact same session again — what a SessionEnd re-run or a
    # `reconcile` re-ingest does in practice.
    temp_store.ingest_session(session, events)
    p2 = temp_store.sqlite.get_project(project_id)

    assert p2["session_count"] == 1  # still one distinct session, not 2
    assert p2["total_edits"] == edits_after_first  # edits not double-counted


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


def test_skip_analysis_populates_sqlite_without_vectors(sample_session_file, temp_store):
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
    # Inline imports to keep this test self-contained; ruff I001 wants
    # stdlib first, then local.
    import json

    from longhand.storage import LonghandStore
    from longhand.storage.store import _build_episode_text

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
    batched_result = batched.ingest_session(session, events, run_analysis=True)
    batched_episode_ids = set(batched.vectors.episodes_collection.get()["ids"])
    batched_segment_ids = set(batched.vectors.segments_collection.get()["ids"])

    # v0.5.13 — the ingest result must expose embedded-vector counts so
    # callers can tell how many vectors actually landed in Chroma.
    assert "episodes_embedded" in batched_result
    assert "segments_embedded" in batched_result
    assert batched_result["episodes_embedded"] == len(batched_episode_ids)
    assert batched_result["segments_embedded"] == len(batched_segment_ids)

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


def test_batched_methods_return_upserted_count(temp_store):
    """`add_*_embeddings_batch` returns the count of embeddings actually upserted,
    skipping items with empty/whitespace text."""
    items = [
        {"episode_id": "ep-1", "text": "real problem and fix", "metadata": {"has_fix": True}},
        {"episode_id": "ep-2", "text": "", "metadata": {"has_fix": True}},
        {"episode_id": "ep-3", "text": "   ", "metadata": {"has_fix": True}},
        {"episode_id": "ep-4", "text": "another real fix", "metadata": {"has_fix": True}},
    ]
    assert temp_store.vectors.add_episode_embeddings_batch(items) == 2
    assert temp_store.vectors.add_episode_embeddings_batch([]) == 0

    seg_items = [
        {
            "segment_id": "s-1",
            "text": "topic discussion",
            "metadata": {"segment_type": "discussion"},
        },
        {"segment_id": "s-2", "text": "", "metadata": {"segment_type": "discussion"}},
    ]
    assert temp_store.vectors.add_segment_embeddings_batch(seg_items) == 1


def test_busy_timeout_pragma(temp_store):
    """Write-lock waits must be generous — parallel hooks contend on one DB."""
    with temp_store.sqlite.connect() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000


def test_query_episodes_has_fix_in_sql(temp_store):
    """has_fix filters inside the SQL WHERE (before ORDER BY/LIMIT)."""
    base = {
        "session_id": "s1",
        "project_id": "p1",
        "started_at": "2026-07-01T10:00:00Z",
        "ended_at": "2026-07-01T10:30:00Z",
        "problem_event_id": "pe",
        "problem_description": "x",
        "fix_summary": "",
        "touched_files": [],
        "tags": [],
        "status": "unresolved",
    }
    temp_store.sqlite.insert_episodes(
        [
            {**base, "episode_id": "ep-f", "fix_event_id": "fe-1", "status": "resolved"},
            {**base, "episode_id": "ep-n", "fix_event_id": None},
        ]
    )

    with_fix = temp_store.sqlite.query_episodes(has_fix=True)
    fixless = temp_store.sqlite.query_episodes(has_fix=False)
    everything = temp_store.sqlite.query_episodes()

    assert [e["episode_id"] for e in with_fix] == ["ep-f"]
    assert [e["episode_id"] for e in fixless] == ["ep-n"]
    assert {e["episode_id"] for e in everything} == {"ep-f", "ep-n"}


def test_query_episodes_min_confidence_in_sql(temp_store):
    """min_confidence filters inside the SQL WHERE (before ORDER BY/LIMIT)."""
    base = {
        "session_id": "s1",
        "project_id": "p1",
        "started_at": "2026-07-01T10:00:00Z",
        "ended_at": "2026-07-01T10:30:00Z",
        "problem_event_id": "pe",
        "fix_event_id": "fe",
        "problem_description": "x",
        "fix_summary": "y",
        "touched_files": [],
        "tags": [],
        "status": "resolved",
    }
    temp_store.sqlite.insert_episodes(
        [
            {**base, "episode_id": "ep-hi", "confidence": 0.8},
            {**base, "episode_id": "ep-lo", "confidence": 0.3},
        ]
    )

    confident = temp_store.sqlite.query_episodes(min_confidence=0.5)
    everything = temp_store.sqlite.query_episodes()

    assert [e["episode_id"] for e in confident] == ["ep-hi"]
    assert {e["episode_id"] for e in everything} == {"ep-hi", "ep-lo"}


def test_reanalysis_replaces_stale_episodes(sample_session_file, temp_store):
    """Re-analysis must REPLACE a session's episodes/segments — boundary
    changes mint new ids, and without the pre-delete the old-boundary rows
    accumulate as stale duplicates on every analyze pass."""
    from longhand.parser import JSONLParser

    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events, run_analysis=True)

    # Simulate an old-extractor leftover: same session, id that the current
    # extractor will never mint again.
    temp_store.sqlite.insert_episodes(
        [
            {
                "episode_id": "ep-stale-boundary",
                "session_id": session.session_id,
                "project_id": "p1",
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T00:30:00Z",
                "problem_event_id": "pe-old",
                "fix_event_id": "fe-old",
                "problem_description": "stale",
                "fix_summary": "stale",
                "touched_files": [],
                "tags": [],
                "status": "resolved",
            }
        ]
    )

    temp_store.analyze_session(session, events)

    with temp_store.sqlite.connect() as conn:
        stale = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE episode_id = 'ep-stale-boundary'"
        ).fetchone()[0]
    assert stale == 0

    # And it stays stable across repeated passes (no accumulation).
    with temp_store.sqlite.connect() as conn:
        first = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    temp_store.analyze_session(session, events)
    with temp_store.sqlite.connect() as conn:
        second = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    assert first == second


def test_load_session_from_db_round_trips_analysis_fields(sample_session_file, temp_store):
    """Events rebuilt from the DB must carry the fields episode extraction
    reads (tool_use_id, tool_success, error_detected) — not just the
    attribution subset."""
    from longhand.parser import JSONLParser

    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events, run_analysis=False)

    row = temp_store.sqlite.list_sessions(limit=10)[0]
    loaded = temp_store.load_session_from_db(row)
    assert loaded is not None
    rebuilt_session, rebuilt_events = loaded
    assert rebuilt_session.session_id == session.session_id
    assert len(rebuilt_events) == len(events)

    by_id = {e.event_id: e for e in rebuilt_events}
    for orig in events:
        re = by_id[orig.event_id]
        assert re.tool_use_id == orig.tool_use_id
        assert re.tool_success == orig.tool_success
        assert re.error_detected == orig.error_detected
        assert re.tool_name == orig.tool_name


def test_segment_ids_unique_even_with_duplicate_sequences(temp_store):
    """Events rebuilt from the DB can carry duplicate sequence numbers
    (live-tail + full-ingest rows) — two segments starting at the same
    sequence must not hash to one id, which fails the whole Chroma batch."""
    from datetime import datetime, timezone

    from longhand.analysis.segment_extraction import extract_segments
    from longhand.types import Event, EventType

    def _user(eid: str, seq: int, text: str) -> Event:
        return Event(
            event_id=eid,
            session_id="s-dup",
            event_type=EventType.USER_MESSAGE,
            sequence=seq,
            timestamp=datetime(2026, 7, 1, 0, min(seq, 59), tzinfo=timezone.utc),
            content=text,
        )

    # Two conversation clusters, both starting at sequence 1 (duplicated).
    events = [
        _user("u1", 1, "first topic about the login flow and sessions"),
        _user("u2", 1, "more about login"),
        _user("u3", 40, "totally different second topic about deployment"),
    ]
    # Force a second cluster by making the timestamps far apart if the
    # extractor splits on gaps; regardless, ids must be unique.
    segments = extract_segments("s-dup", "p1", events)
    ids = [s["segment_id"] for s in segments]
    assert len(ids) == len(set(ids))


def test_segment_batch_dedupes_duplicate_ids(temp_store):
    items = [
        {"segment_id": "seg-x", "text": "first version", "metadata": {"segment_type": "d"}},
        {"segment_id": "seg-x", "text": "duplicate id", "metadata": {"segment_type": "d"}},
        {"segment_id": "seg-y", "text": "another segment", "metadata": {"segment_type": "d"}},
    ]
    # Must not raise (Chroma rejects batches with repeated ids wholesale).
    n = temp_store.vectors.add_segment_embeddings_batch(items)
    assert n == 2
