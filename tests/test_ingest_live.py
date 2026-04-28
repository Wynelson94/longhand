"""Tests for live (Stop-hook) ingestion.

Live ingest tails new bytes from a Claude Code transcript JSONL on every
assistant turn. These tests pin:

- the stdin contract (mirrors ingest-session)
- offset advance is monotonic and survives partial trailing lines
- analysis (episodes/segments/embeddings) is NOT run on the live path
- lock contention is non-blocking
- plans_index view returns Write/Edit events to ~/.claude/plans/*.md
- live + SessionEnd compose: live tails events; SessionEnd fills analysis
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from longhand.cli import app
from longhand.parser import JSONLParser
from longhand.setup_commands import ingest_live_tail
from longhand.storage import LonghandStore


def _line(entry: dict) -> str:
    return json.dumps(entry) + "\n"


def _make_turn(uuid: str, parent: str | None, ts: str, text: str = "hello") -> list[dict]:
    """One user prompt + one assistant text reply at timestamp `ts`."""
    return [
        {
            "type": "user",
            "uuid": f"u-{uuid}",
            "parentUuid": parent,
            "sessionId": "live-test",
            "timestamp": ts,
            "cwd": "/tmp/p",
            "isSidechain": False,
            "message": {"role": "user", "content": text},
        },
        {
            "type": "assistant",
            "uuid": f"a-{uuid}",
            "parentUuid": f"u-{uuid}",
            "sessionId": "live-test",
            "timestamp": ts,
            "cwd": "/tmp/p",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            },
        },
    ]


def test_ingest_live_reads_transcript_from_stdin(tmp_path: Path) -> None:
    """No --transcript → command reads transcript_path from stdin JSON."""
    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))

    runner = CliRunner()
    data_dir = tmp_path / "longhand"

    payload = json.dumps({"transcript_path": str(transcript)})
    result = runner.invoke(
        app,
        ["ingest-live", "--data-dir", str(data_dir)],
        input=payload,
    )

    assert result.exit_code == 0, result.output
    db = data_dir / "longhand.db"
    assert db.exists()


def test_ingest_live_silent_on_empty_stdin(tmp_path: Path) -> None:
    """Empty stdin → exit 0 silently (must not crash hook chain)."""
    runner = CliRunner()
    data_dir = tmp_path / "longhand"
    result = runner.invoke(
        app, ["ingest-live", "--data-dir", str(data_dir)], input=""
    )
    assert result.exit_code == 0


def test_ingest_live_silent_on_malformed_stdin(tmp_path: Path) -> None:
    runner = CliRunner()
    data_dir = tmp_path / "longhand"
    result = runner.invoke(
        app,
        ["ingest-live", "--data-dir", str(data_dir)],
        input="{{ not json",
    )
    assert result.exit_code == 0


def test_ingest_live_advances_offset_then_caught_up(tmp_path: Path) -> None:
    """First call ingests events; second call is a no-op (caught up)."""
    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))

    data_dir = tmp_path / "longhand"

    summary1 = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert summary1["events"] >= 1
    assert summary1["session_id"] == "live-test"
    assert summary1["advanced_to"] == transcript.stat().st_size

    summary2 = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert summary2["skipped"] == "caught-up"


def test_ingest_live_appends_only_new_events(tmp_path: Path) -> None:
    """After appending another turn, second live call ingests only the new events."""
    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))

    data_dir = tmp_path / "longhand"
    ingest_live_tail(str(transcript), data_dir=str(data_dir))

    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        n_first = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = 'live-test'"
        ).fetchone()[0]

    with transcript.open("a") as f:
        for entry in _make_turn("2", "a-1", "2026-04-28T10:00:01.000Z"):
            f.write(_line(entry))

    summary = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert summary["events"] >= 1, summary

    with store.sqlite.connect() as conn:
        n_after = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = 'live-test'"
        ).fetchone()[0]

    assert n_after > n_first


def test_ingest_live_partial_trailing_line_held_back(tmp_path: Path) -> None:
    """A trailing line missing its newline is treated as in-flight and re-read later."""
    transcript = tmp_path / "session.jsonl"
    entries = _make_turn("1", None, "2026-04-28T10:00:00.000Z")

    # Write all but the last entry's newline so the last line is "partial."
    with transcript.open("w") as f:
        f.write(_line(entries[0]))
        f.write(json.dumps(entries[1]))  # NO trailing \n

    data_dir = tmp_path / "longhand"
    summary1 = ingest_live_tail(str(transcript), data_dir=str(data_dir))

    # Offset must NOT have advanced past the first line's newline by more than its length.
    first_size = len(_line(entries[0]))
    assert summary1["advanced_to"] == first_size
    # The first complete line should have produced at least one event.
    assert summary1["events"] >= 1

    # Now finalize the second line by appending the missing newline.
    with transcript.open("a") as f:
        f.write("\n")

    summary2 = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert summary2["events"] >= 1
    assert summary2["advanced_to"] == transcript.stat().st_size


def test_ingest_live_skips_episode_and_segment_extraction(tmp_path: Path) -> None:
    """Live path must NOT populate episodes or segments (those are SessionEnd's job)."""
    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))

    data_dir = tmp_path / "longhand"
    ingest_live_tail(str(transcript), data_dir=str(data_dir))

    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        ep = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE session_id = 'live-test'"
        ).fetchone()[0]
        seg = conn.execute(
            "SELECT COUNT(*) FROM conversation_segments WHERE session_id = 'live-test'"
        ).fetchone()[0]
        outc = conn.execute(
            "SELECT COUNT(*) FROM session_outcomes WHERE session_id = 'live-test'"
        ).fetchone()[0]

    assert ep == 0
    assert seg == 0
    assert outc == 0


def test_ingest_live_non_blocking_on_lock_contention(tmp_path: Path) -> None:
    """If the ingest lock is held by another alive PID, live ingest exits silently."""
    import subprocess
    import time

    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))

    data_dir = tmp_path / "longhand"
    LonghandStore(data_dir=data_dir)

    # Spawn a real process so its PID answers alive to os.kill(pid, 0).
    proc = subprocess.Popen(["/bin/sleep", "5"])
    try:
        # Give the kernel a beat to register the PID.
        time.sleep(0.05)
        lock = data_dir / ".ingest.lock"
        lock.write_text(str(proc.pid))

        summary = ingest_live_tail(str(transcript), data_dir=str(data_dir))
        assert summary["skipped"] == "locked", summary
    finally:
        proc.kill()
        proc.wait()


def test_plans_index_view_lists_plan_writes(tmp_path: Path) -> None:
    """plans_index view returns every Write/Edit to a ~/.claude/plans/*.md file."""
    transcript = tmp_path / "session.jsonl"

    plans_path = "/Users/test/.claude/plans/foo.md"
    entries: list[dict] = []
    for i, content in enumerate(["v1", "v2", "v3"]):
        entries.append(
            {
                "type": "assistant",
                "uuid": f"a-plan-{i}",
                "parentUuid": None,
                "sessionId": "plan-session",
                "timestamp": f"2026-04-28T10:00:0{i}.000Z",
                "cwd": "/tmp/p",
                "isSidechain": False,
                "message": {
                    "model": "claude-sonnet-4-6",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"tool-plan-{i}",
                            "name": "Write",
                            "input": {"file_path": plans_path, "content": content},
                        }
                    ],
                },
            }
        )

    with transcript.open("w") as f:
        for entry in entries:
            f.write(_line(entry))

    data_dir = tmp_path / "longhand"
    # Use the full ingest path so plans get persisted with file_operation set.
    parser = JSONLParser(transcript)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store = LonghandStore(data_dir=data_dir)
    store.ingest_session(session, events, run_analysis=False)

    rows = store.sqlite.list_plans(limit=10)
    assert len(rows) == 3
    assert all(r["file_path"] == plans_path for r in rows)
    # Newest first
    assert rows[0]["timestamp"] >= rows[-1]["timestamp"]


def test_migration_v5_adds_last_offset_column(tmp_path: Path) -> None:
    """ingestion_log gains last_offset, and existing rows backfill from file_size."""
    data_dir = tmp_path / "longhand"
    store = LonghandStore(data_dir=data_dir)

    with store.sqlite.connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ingestion_log)").fetchall()}
        views = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'view'"
            ).fetchall()
        }

    assert "last_offset" in cols
    assert "plans_index" in views


def test_live_then_session_end_composes(tmp_path: Path) -> None:
    """Live tail keeps events fresh; SessionEnd fills in episodes/segments later."""
    transcript = tmp_path / "session.jsonl"
    # Two turns written incrementally, then a final SessionEnd-style ingest.
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))
    data_dir = tmp_path / "longhand"
    s1 = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert s1["events"] >= 1

    with transcript.open("a") as f:
        for entry in _make_turn("2", "a-1", "2026-04-28T10:00:01.000Z"):
            f.write(_line(entry))
    s2 = ingest_live_tail(str(transcript), data_dir=str(data_dir))
    assert s2["events"] >= 1
    assert s2["advanced_to"] == transcript.stat().st_size

    # SessionEnd's full pass should be safe to run on top — idempotent upserts.
    parser = JSONLParser(transcript)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store = LonghandStore(data_dir=data_dir)
    store.ingest_session(session, events, run_analysis=False)

    with store.sqlite.connect() as conn:
        n_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE session_id = 'live-test' "
            "AND event_id NOT LIKE '%#%'"
        ).fetchone()[0]
        size_logged = conn.execute(
            "SELECT file_size FROM ingestion_log WHERE transcript_path = ?",
            (str(transcript),),
        ).fetchone()[0]

    assert n_events == len(events)
    assert size_logged == transcript.stat().st_size
