"""Tests for live (Stop-hook) ingestion.

Live ingest tails new bytes from a Claude Code transcript JSONL on every
assistant turn. These tests pin:

- the stdin contract (mirrors ingest-session)
- offset advance is monotonic and survives partial trailing lines
- analysis (episodes/segments/embeddings) is NOT run on the live path
- lock contention is non-blocking
- a Stop hook that finds the lock busy hands its turn to one detached
  waiter per transcript, which reads the tail once the lock frees
- plans_index view returns Write/Edit events to ~/.claude/plans/*.md
- live + SessionEnd compose: live tails events; SessionEnd fills analysis
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
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
    result = runner.invoke(app, ["ingest-live", "--data-dir", str(data_dir)], input="")
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
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()
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
            "SELECT COUNT(*) FROM events WHERE session_id = 'live-test' AND event_id NOT LIKE '%#%'"
        ).fetchone()[0]
        size_logged = conn.execute(
            "SELECT file_size FROM ingestion_log WHERE transcript_path = ?",
            (str(transcript),),
        ).fetchone()[0]

    assert n_events == len(events)
    assert size_logged == transcript.stat().st_size


def test_live_tail_never_raises_when_store_init_fails(tmp_path, monkeypatch):
    """Store construction failures must surface as a summary, never an exception.

    The Stop hook calls this every assistant turn — a corrupted Chroma dir or
    a migration race must not crash the hook chain.
    """
    entry = {
        "type": "user",
        "uuid": "u-x",
        "sessionId": "s-x",
        "timestamp": "2026-01-01T00:00:00Z",
        "cwd": "/tmp/p",
        "message": {"role": "user", "content": "hi"},
    }
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_line(entry))

    def boom(*args, **kwargs):
        raise RuntimeError("corrupted store")

    monkeypatch.setattr("longhand.setup_commands.LonghandStore", boom)

    summary = ingest_live_tail(str(transcript), data_dir=str(tmp_path / "lh"))
    assert summary["skipped"] == "error:RuntimeError"


def test_live_tail_never_raises_when_caught_up_check_fails(tmp_path, monkeypatch):
    """Failures after store construction but before the lock also return a summary."""

    class _BrokenSqlite:
        def live_caught_up(self, *args, **kwargs):
            raise RuntimeError("db exploded")

    class _BrokenStore:
        def __init__(self, *args, **kwargs):
            self.sqlite = _BrokenSqlite()

    monkeypatch.setattr("longhand.setup_commands.LonghandStore", _BrokenStore)

    entry = {
        "type": "user",
        "uuid": "u-y",
        "sessionId": "s-y",
        "timestamp": "2026-01-01T00:00:00Z",
        "cwd": "/tmp/p",
        "message": {"role": "user", "content": "hi"},
    }
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(_line(entry))

    summary = ingest_live_tail(str(transcript), data_dir=None)
    assert summary["skipped"] == "error:RuntimeError"


# ─── Background retry when the Stop hook finds the lock busy ─────────────────
#
# The 2026-11-04 incident: a Stop hook found the lock held by a background
# ingest and deferred to "the next Stop", which never came because the next
# turn was cut off. The hook now hands the turn to a detached waiter.


def _locked_store(tmp_path: Path, holder_pid: int) -> tuple[Path, Path]:
    """A one-turn transcript, and a store whose ingest lock `holder_pid` holds."""
    transcript = tmp_path / "session.jsonl"
    with transcript.open("w") as f:
        for entry in _make_turn("1", None, "2026-04-28T10:00:00.000Z"):
            f.write(_line(entry))
    data_dir = tmp_path / "longhand"
    LonghandStore(data_dir=data_dir)
    (data_dir / ".ingest.lock").write_text(str(holder_pid))
    return transcript, data_dir


def _stop_hook(transcript: Path, data_dir: Path):
    """Run `ingest-live` the way Claude Code's Stop hook does: JSON on stdin."""
    return CliRunner().invoke(
        app,
        ["ingest-live", "--data-dir", str(data_dir)],
        input=json.dumps({"transcript_path": str(transcript)}),
    )


def _retry_markers(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "live-retry").glob("*.pid"))


def _reap(proc: subprocess.Popen) -> None:
    # An unreaped zombie still answers "alive" to the lock's liveness probe.
    proc.kill()
    proc.wait()


def test_stop_hook_lockout_spawns_a_background_retry(tmp_path: Path) -> None:
    transcript, data_dir = _locked_store(tmp_path, os.getppid())  # a live holder

    with patch("subprocess.Popen") as popen:
        popen.return_value.pid = 424242
        result = _stop_hook(transcript, data_dir)

    assert result.exit_code == 0, result.output
    assert result.output == ""  # the hook stays silent
    assert popen.call_count == 1
    assert popen.call_args.args[0] == [
        sys.executable,
        "-m",
        "longhand",
        "ingest-live",
        "--transcript",
        str(transcript),
        "--wait-lock",
        "600",
        "--data-dir",
        str(data_dir),
    ]
    # Detached, so it outlives the hook and the session; logged where
    # lock-out frequency can be counted.
    kwargs = popen.call_args.kwargs
    assert kwargs["start_new_session"] is True
    assert Path(kwargs["stdout"].name).name.startswith("live-retry-")
    # Started in the data dir, not the session's project dir: with `-m`, Python
    # imports a `longhand/` package from the working directory first, so a
    # session inside a Longhand checkout would otherwise run that code.
    assert kwargs.get("cwd") == str(data_dir)
    assert kwargs.get("env", {}).get("LONGHAND_DATA_DIR") == str(data_dir)
    assert kwargs.get("stdin") == subprocess.DEVNULL
    # The marker names the waiter, so the next locked turn knows one is coming.
    assert [m.read_text() for m in _retry_markers(data_dir)] == ["424242"]


def test_live_retry_paths_are_absolute_when_the_data_dir_is_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The waiter starts in the data dir, so a relative path would point
    somewhere else from there."""
    transcript, data_dir = _locked_store(tmp_path, os.getppid())
    monkeypatch.chdir(tmp_path)

    with patch("subprocess.Popen") as popen:
        _stop_hook(transcript, Path(data_dir.name))  # --data-dir longhand

    assert popen.call_count == 1
    argv = popen.call_args.args[0]
    passed = Path(argv[argv.index("--data-dir") + 1])
    assert passed.is_absolute()
    assert passed.resolve() == data_dir.resolve()
    assert Path(popen.call_args.kwargs.get("cwd") or ".").resolve() == data_dir.resolve()


def test_direct_live_tail_never_spawns_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the hook spawns. A direct call or an explicit --transcript run
    reports the busy lock and stops."""
    from longhand.recall import project_fallback

    transcript, data_dir = _locked_store(tmp_path, os.getppid())
    claims: list[bool] = []
    real_claim = project_fallback.claim_ingest_lock

    def recording_claim(store):
        claims.append(real_claim(store))
        return claims[-1]

    monkeypatch.setattr(project_fallback, "claim_ingest_lock", recording_claim)

    with patch("subprocess.Popen") as popen:
        summary = ingest_live_tail(str(transcript), data_dir=str(data_dir))
        result = CliRunner().invoke(
            app, ["ingest-live", "--transcript", str(transcript), "--data-dir", str(data_dir)]
        )

    assert summary["skipped"] == "locked"
    assert result.exit_code == 0, result.output
    assert claims == [False, False]  # both runs reached the busy lock
    popen.assert_not_called()
    assert _retry_markers(data_dir) == []


def test_live_retry_dedupes_on_a_live_waiter_and_replaces_a_dead_one(tmp_path: Path) -> None:
    """One waiter per transcript: it reads to EOF once it has the lock, so it
    covers later locked turns too. A marker whose waiter died is replaced."""
    transcript, data_dir = _locked_store(tmp_path, os.getppid())
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()  # reaped, so its PID answers dead

    with patch("subprocess.Popen") as popen:
        popen.return_value.pid = os.getppid()  # a waiter that is still alive
        _stop_hook(transcript, data_dir)
        _stop_hook(transcript, data_dir)
        assert popen.call_count == 1

        (marker,) = _retry_markers(data_dir)
        marker.write_text(str(gone.pid))  # that waiter died without cleaning up
        _stop_hook(transcript, data_dir)
        assert popen.call_count == 2

    assert marker.read_text() == str(os.getppid())


def test_live_retry_replaces_a_marker_older_than_any_wait(tmp_path: Path) -> None:
    """A killed waiter leaves its marker behind, and if its PID is then reused
    the marker still answers alive. No waiter waits past the retry window, so
    a marker older than that is stale whatever its PID says."""
    import time

    transcript, data_dir = _locked_store(tmp_path, os.getppid())

    with patch("subprocess.Popen") as popen:
        popen.return_value.pid = os.getppid()  # alive, like a reused PID
        _stop_hook(transcript, data_dir)
        (marker,) = _retry_markers(data_dir)
        old = time.time() - 13 * 60
        os.utime(marker, (old, old))
        _stop_hook(transcript, data_dir)

    assert popen.call_count == 2


def test_live_retry_waiter_clears_its_marker_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn locked out after the waiter has read must start a retry of its
    own, so the waiter gives up its marker before it reads, not when it exits."""
    transcript, data_dir = _locked_store(tmp_path, os.getppid())
    with patch("subprocess.Popen") as popen:
        popen.return_value.pid = os.getpid()  # the waiter runs in this process
        _stop_hook(transcript, data_dir)
    (marker,) = _retry_markers(data_dir)
    (data_dir / ".ingest.lock").unlink()  # the holder has finished

    marker_at_read: list[bool] = []
    real_tail = JSONLParser.parse_tail_from_offset

    def tail_noting_the_marker(self, *args, **kwargs):
        marker_at_read.append(marker.exists())
        return real_tail(self, *args, **kwargs)

    monkeypatch.setattr(JSONLParser, "parse_tail_from_offset", tail_noting_the_marker)
    summary = ingest_live_tail(str(transcript), data_dir=str(data_dir), wait_lock_s=5)

    assert summary["events"] >= 1, summary
    assert marker_at_read == [False]


def test_live_retry_waiter_reads_the_tail_once_the_lock_frees(tmp_path: Path) -> None:
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        transcript, data_dir = _locked_store(tmp_path, holder.pid)
        with patch("subprocess.Popen") as popen:
            popen.return_value.pid = os.getpid()  # the waiter runs in this process
            _stop_hook(transcript, data_dir)
        assert popen.call_count == 1

        # Run the waiter the hook spawned, with a test-sized wait. Its lock
        # holder finishes a second in.
        argv = popen.call_args.args[0][3:]  # drop `python -m longhand`
        argv[argv.index("--wait-lock") + 1] = "30"
        threading.Timer(1.0, _reap, args=(holder,)).start()
        result = CliRunner().invoke(app, argv)
    finally:
        _reap(holder)

    assert result.exit_code == 0, result.output
    (line,) = result.output.splitlines()
    assert "ingest-live retry: session.jsonl +2 events after waiting" in line
    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        ids = {r[0] for r in conn.execute("SELECT event_id FROM events")}
    assert {"u-1", "a-1"} <= ids
    assert _retry_markers(data_dir) == []  # cleared once the waiter had the lock
    assert not (data_dir / ".ingest.lock").exists()


def test_live_retry_waiter_timeout_leaves_a_breadcrumb(tmp_path: Path) -> None:
    """A waiter that never gets the lock gives up quietly. The breadcrumb's
    class is one reconcile heals once the transcript has gone quiet."""
    transcript, data_dir = _locked_store(tmp_path, os.getppid())  # never lets go

    result = CliRunner().invoke(
        app,
        [
            "ingest-live",
            "--transcript",
            str(transcript),
            "--wait-lock",
            "1",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "ingest-live retry: session.jsonl lock-timeout after waiting" in result.output
    (log,) = (data_dir / "logs").glob("hook-errors-*.log")
    assert f"ingest-live lock-timeout: {transcript}" in log.read_text()
    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
