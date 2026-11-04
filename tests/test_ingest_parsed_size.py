"""A full ingest records only the bytes it parsed.

A full ingest of a transcript that is still being written takes seconds
(embedding, analysis). It used to measure the file's size at the end and log
that as ingested, so a turn appended mid-ingest was claimed without being
parsed: the live tail's cursor jumped past it, and reconcile's
changed-since-last-full-ingest check (which compares the same size) saw
nothing to heal. In the 2026-11-04 incident the lock holder was exactly such
an ingest. The size is now taken before parsing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand.cli import app
from longhand.recall import reconcile as reconcile_mod
from longhand.setup_commands import ingest_live_tail, ingest_single_session
from longhand.storage import LonghandStore
from longhand.storage.vector_store import VectorStore


def _turn(n: int) -> str:
    """One user prompt (u-N) and one assistant reply (a-N), as JSONL lines."""
    common = {
        "sessionId": "parsed-size",
        "timestamp": f"2026-11-03T23:{10 + n:02d}:00.000Z",
        "cwd": "/Users/tester/test-project",
        "isSidechain": False,
    }
    user = {
        **common,
        "type": "user",
        "uuid": f"u-{n}",
        "parentUuid": f"a-{n - 1}" if n > 1 else None,
        "message": {"role": "user", "content": f"turn {n}"},
    }
    assistant = {
        **common,
        "type": "assistant",
        "uuid": f"a-{n}",
        "parentUuid": f"u-{n}",
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [{"type": "text", "text": f"reply {n}"}],
        },
    }
    return json.dumps(user) + "\n" + json.dumps(assistant) + "\n"


def _session_end(transcript: Path, data_dir: Path) -> None:
    ingest_single_session(str(transcript), str(data_dir))


def _bulk_ingest(transcript: Path, data_dir: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", str(transcript), "--data-dir", str(data_dir)])
    assert result.exit_code == 0, result.output


def _reconcile_fix(transcript: Path, data_dir: Path) -> None:
    store = LonghandStore(data_dir=data_dir)
    codex_home = data_dir.parent / "no-codex"
    codex_home.mkdir(exist_ok=True)
    original = reconcile_mod.discover_sessions
    reconcile_mod.discover_sessions = lambda: [transcript]
    try:
        report = reconcile_mod.run_reconcile(store, fix=True, codex_home=codex_home)
    finally:
        reconcile_mod.discover_sessions = original
    assert report.ingested == 1, report.to_dict()


@pytest.mark.parametrize(
    "full_ingest",
    [_session_end, _bulk_ingest, _reconcile_fix],
    ids=["session-end", "bulk-ingest", "reconcile-fix"],
)
def test_a_turn_written_during_a_full_ingest_is_not_logged_as_ingested(
    full_ingest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(_turn(1))
    parsed_size = transcript.stat().st_size
    data_dir = tmp_path / "lh"

    # The session writes its next turn while the ingest is embedding.
    real_add_events = VectorStore.add_events
    appended: list[bool] = []

    def add_events_while_the_session_writes(self, events):
        if not appended:
            with transcript.open("a") as f:
                f.write(_turn(2))
            appended.append(True)
        return real_add_events(self, events)

    monkeypatch.setattr(VectorStore, "add_events", add_events_while_the_session_writes)
    full_ingest(transcript, data_dir)
    assert appended, "the ingest never embedded, so the race did not happen"

    store = LonghandStore(data_dir=data_dir)
    size_now = transcript.stat().st_size
    with store.sqlite.connect() as conn:
        logged = conn.execute(
            "SELECT file_size, last_offset FROM ingestion_log WHERE transcript_path = ?",
            (str(transcript),),
        ).fetchone()
    assert (logged["file_size"], logged["last_offset"]) == (parsed_size, parsed_size)
    assert not store.sqlite.already_ingested(str(transcript), size_now)

    # The next Stop hook's tail picks up the turn the ingest never read.
    summary = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert summary["skipped"] is None, summary
    with store.sqlite.connect() as conn:
        ids = {r[0] for r in conn.execute("SELECT event_id FROM events")}
    assert {"u-2", "a-2"} <= ids
