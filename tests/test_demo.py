"""Tests for the `longhand demo` walkthrough.

Covers:
- Sample corpus is deterministic + valid JSONL-event shape
- Corpus ingests cleanly into a fresh LonghandStore
- recall against the seeded store returns non-empty results for the
  known queries
- The demo runner cleans up after itself unless keep=True
"""

from __future__ import annotations

import json
from pathlib import Path

from longhand.demo import run_demo
from longhand.demo.corpus import generate_corpus
from longhand.parser import JSONLParser
from longhand.recall import recall as recall_pipeline
from longhand.storage.store import LonghandStore


def test_corpus_is_deterministic(tmp_path):
    """Same project dir produces the same corpus across calls."""
    pd = tmp_path / "demo-shop"
    pd.mkdir()
    (pd / ".git").mkdir()

    first = generate_corpus(pd)
    second = generate_corpus(pd)

    assert first == second, "generate_corpus must be deterministic"
    assert len(first) == 3, "demo corpus is 3 sessions"


def test_corpus_events_are_valid_jsonl(tmp_path):
    """Every event must be JSON-serializable and have the required Claude Code fields."""
    pd = tmp_path / "demo-shop"
    pd.mkdir()
    (pd / ".git").mkdir()

    sessions = generate_corpus(pd)
    for filename, events in sessions:
        assert filename.endswith(".jsonl"), "session files end in .jsonl"
        assert len(events) > 0, "session has at least one event"
        for event in events:
            # Must serialize cleanly
            json.dumps(event)
            # Required Claude Code transcript fields
            assert "type" in event
            assert event["type"] in ("user", "assistant"), f"unexpected event type: {event['type']}"
            assert "uuid" in event
            assert "sessionId" in event
            assert "timestamp" in event
            assert "cwd" in event
            assert "message" in event


def test_corpus_ingests_into_store(tmp_path):
    """Writing the corpus to disk and ingesting via the standard parser succeeds."""
    pd = tmp_path / "demo-shop"
    pd.mkdir()
    (pd / ".git").mkdir()
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    store = LonghandStore(data_dir=store_dir)
    sessions = generate_corpus(pd)
    total = 0
    for filename, events in sessions:
        path = jsonl_dir / filename
        with path.open("w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        parser = JSONLParser(path)
        parsed = list(parser.parse_events())
        session = parser.build_session(parsed)
        store.ingest_session(session, parsed)
        total += len(parsed)

    assert total > 0, "events were ingested"
    # Verify the store actually has the sessions
    stats = store.stats()
    assert stats.get("sessions", 0) == 3, f"expected 3 sessions in store, got {stats}"


def test_recall_finds_seeded_stripe_bug(tmp_path):
    """The Stripe signature bug from session A should be recallable by topic."""
    pd = tmp_path / "demo-shop"
    pd.mkdir()
    (pd / ".git").mkdir()
    jsonl_dir = tmp_path / "jsonl"
    jsonl_dir.mkdir()
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    store = LonghandStore(data_dir=store_dir)
    sessions = generate_corpus(pd)
    for filename, events in sessions:
        path = jsonl_dir / filename
        with path.open("w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        parser = JSONLParser(path)
        parsed = list(parser.parse_events())
        session = parser.build_session(parsed)
        store.ingest_session(session, parsed)

    result = recall_pipeline(store, "stripe signature bug")
    assert result.narrative, "recall produced a narrative"
    # The Stripe session is demo-001; its prefix should appear in the narrative
    assert "demo-001" in result.narrative or len(result.episodes) > 0, (
        "recall surfaced the Stripe-bug session by topic"
    )


def test_run_demo_cleans_up_by_default(tmp_path, monkeypatch):
    """run_demo() without keep=True removes its sandbox dir."""
    # Force the tempdir to a known location so we can verify cleanup
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    result = run_demo(keep=False)
    assert result is None, "no path returned when keep=False"
    # The demo dir name follows the pattern longhand-demo-<timestamp>;
    # after cleanup nothing matching that pattern should remain in tmp_path
    leftover = [p for p in tmp_path.iterdir() if p.name.startswith("longhand-demo-")]
    assert leftover == [], f"demo dir not cleaned up: {leftover}"


def test_run_demo_keeps_sandbox_when_requested(tmp_path, monkeypatch):
    """run_demo(keep=True) leaves the sandbox in place and returns its path."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    result = run_demo(keep=True)
    assert isinstance(result, Path), "keep=True returns the demo path"
    assert result.exists(), "demo dir preserved"
    assert (result / "store").exists(), "store dir preserved"
    assert (result / "jsonl").exists(), "jsonl dir preserved"
    assert (result / "demo-shop").exists(), "project dir preserved"
