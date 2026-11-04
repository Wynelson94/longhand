"""Reconcile heals a transcript that grew after its last full ingest.

The 2026-11-04 incident: a session's Stop hook found the ingest lock held by a
background ingest and skipped ("defer to next Stop"). The next turn was cut
off, so neither a Stop hook nor SessionEnd ever captured the tail. Reconcile
counted the session as fully indexed because its row existed with a project,
and the lost turns stayed invisible until a chance bulk ingest.

These tests pin the backstop. A transcript whose size differs from its last
full ingest is `changed` once it has been quiet for the finalize window, and
--fix re-ingests it. While it is still being written it is `settling` and
left to its own session's hooks.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand import mcp_server
from longhand.cli import app
from longhand.parser import JSONLParser
from longhand.recall import reconcile as reconcile_mod
from longhand.recall.reconcile import run_reconcile
from longhand.setup_commands import ingest_live_tail

SESSION_ID = "reconcile-changed-1"
# A cwd that project inference attributes (the conftest sample uses it too).
# A NULL project_id would put the file in reconcile's null_project bucket,
# which re-ingests it on main as well and would hide the bug under test.
CWD = "/Users/tester/test-project"
QUIET = 31 * 60  # just past the 30-minute finalize window


def _turn(n: int, text: str, session_id: str = SESSION_ID) -> list[dict]:
    """One user prompt (event id u-N) and one assistant text reply (a-N)."""
    ts = f"2026-11-03T23:{10 + n:02d}:00.000Z"
    common = {
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": CWD,
        "gitBranch": "main",
        "isSidechain": False,
    }
    return [
        {
            **common,
            "type": "user",
            "uuid": f"u-{n}",
            "parentUuid": f"a-{n - 1}" if n > 1 else None,
            "message": {"role": "user", "content": f"turn {n}"},
        },
        {
            **common,
            "type": "assistant",
            "uuid": f"a-{n}",
            "parentUuid": f"u-{n}",
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
    ]


def _append(path: Path, entries: list[dict]) -> None:
    with path.open("a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _full_ingest(store, path: Path, run_analysis: bool = True) -> None:
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    store.ingest_session(parser.build_session(events), events, run_analysis=run_analysis)


def _backdate(path: Path, seconds: int) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _assistant_texts(store, session_id: str = SESSION_ID) -> list[str]:
    with store.sqlite.connect() as conn:
        rows = conn.execute(
            "SELECT content FROM events WHERE session_id = ? AND event_type = 'assistant_text'",
            (session_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _project_id(store, path: Path) -> str | None:
    with store.sqlite.connect() as conn:
        row = conn.execute(
            "SELECT project_id FROM sessions WHERE transcript_path = ?", (str(path),)
        ).fetchone()
    return row[0] if row else None


def _stage(store, path: Path) -> str | None:
    return store.sqlite.analysis_stages().get(str(path))


def _embedded(store, event_id: str) -> bool:
    return bool(store.vectors.events_collection.get(ids=[event_id])["ids"])


@pytest.fixture
def transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One session transcript with a first turn, the only file reconcile sees."""
    path = tmp_path / "session.jsonl"
    _append(path, _turn(1, "starting the check"))
    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [path])
    return path


def test_reconcile_heals_a_tail_the_locked_stop_hook_skipped(temp_store, transcript):
    """The incident end to end: a turn lands while another process holds the
    ingest lock, so the Stop hook's live tail skips, and no later turn comes.
    Once the file is quiet, reconcile --fix must store the lost reply."""
    _full_ingest(temp_store, transcript)
    assert _project_id(temp_store, transcript) is not None
    _append(transcript, _turn(2, "the reply the locked-out Stop hook skipped."))

    # A real process, so its PID answers "alive" to the lock's liveness probe.
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        (temp_store.data_dir / ".ingest.lock").write_text(str(holder.pid))
        summary = ingest_live_tail(str(transcript), data_dir=str(temp_store.data_dir))
        assert summary["skipped"] == "locked", summary
    finally:
        holder.kill()
        holder.wait()  # reaped: an unreaped zombie still answers "alive"

    _backdate(transcript, QUIET)
    fixed = run_reconcile(temp_store, fix=True)
    assert fixed.errors == []
    assert fixed.ingested == 1
    assert "the reply the locked-out Stop hook skipped." in _assistant_texts(temp_store)

    after = run_reconcile(temp_store, fix=False).to_dict()
    assert after.get("changed_count") == 0
    assert after["fully_indexed"] == 1


def test_dry_run_reports_a_quiet_grown_transcript_as_changed(temp_store, transcript):
    """Without --fix, reconcile names the grown file instead of calling it
    fully indexed, and the MCP payload carries the same counts."""
    _full_ingest(temp_store, transcript)
    assert _project_id(temp_store, transcript) is not None
    _append(transcript, _turn(2, "a reply nobody captured"))
    _backdate(transcript, QUIET)

    payload = run_reconcile(temp_store, fix=False).to_dict()
    assert payload.get("changed") == [str(transcript)]
    assert payload.get("changed_count") == 1
    assert payload.get("settling_count") == 0
    assert payload["fully_indexed"] == 0
    assert payload["ingested"] == 0

    result = asyncio.run(mcp_server._tool_reconcile(temp_store, {"fix": False}))
    mcp_payload = json.loads(result[0].text)
    assert mcp_payload.get("changed_count") == 1
    assert mcp_payload.get("settling_count") == 0


def test_active_grown_transcript_is_settling_and_left_to_its_hooks(temp_store, transcript):
    """A transcript written moments ago belongs to a live session whose own
    Stop and SessionEnd hooks will capture it: reconcile counts it as
    settling, doesn't call it fully indexed, and --fix leaves it alone."""
    _full_ingest(temp_store, transcript)
    assert _project_id(temp_store, transcript) is not None
    _append(transcript, _turn(2, "still being written"))

    payload = run_reconcile(temp_store, fix=False).to_dict()
    assert payload.get("settling_count") == 1
    assert payload.get("changed_count") == 0
    assert payload["fully_indexed"] == 0

    fixed = run_reconcile(temp_store, fix=True)
    assert fixed.ingested == 0
    assert "still being written" not in _assistant_texts(temp_store)


def test_live_tail_to_eof_does_not_count_as_a_full_ingest(temp_store, transcript):
    """The live tail stores events but never embeds them and never moves the
    recorded file size. A quiet file the tail has read to the end is still
    behind its last full ingest, so the check must compare file sizes, not
    the live cursor. --fix then gives the tailed reply its embedding."""
    _full_ingest(temp_store, transcript)
    _append(transcript, _turn(2, "tailed but never embedded"))
    summary = ingest_live_tail(str(transcript), data_dir=str(temp_store.data_dir))
    assert summary["skipped"] is None and summary["events"] == 2, summary
    assert temp_store.sqlite.live_caught_up(str(transcript), transcript.stat().st_size)
    assert _project_id(temp_store, transcript) is not None
    assert not _embedded(temp_store, "a-2")
    _backdate(transcript, QUIET)

    assert run_reconcile(temp_store, fix=False).to_dict().get("changed") == [str(transcript)]

    fixed = run_reconcile(temp_store, fix=True)
    assert fixed.ingested == 1
    assert _embedded(temp_store, "a-2")
    assert _stage(temp_store, transcript) == "analyzed"


def test_changed_transcript_keeps_a_deliberate_skip_analysis(temp_store, transcript):
    """Stage 'events' means the last full ingest skipped analysis on purpose,
    which `longhand analyze --all` owns. --fix heals the missing events but
    must not run that deferred analysis."""
    _full_ingest(temp_store, transcript)  # analyzed: attributes the project
    _append(transcript, _turn(2, "second"))
    _full_ingest(temp_store, transcript, run_analysis=False)
    assert _stage(temp_store, transcript) == "events"
    assert _project_id(temp_store, transcript) is not None
    _append(transcript, _turn(3, "after the skip"))
    _backdate(transcript, QUIET)

    assert run_reconcile(temp_store, fix=False).to_dict().get("changed") == [str(transcript)]

    fixed = run_reconcile(temp_store, fix=True)
    assert fixed.ingested == 1
    assert "after the skip" in _assistant_texts(temp_store)
    assert _stage(temp_store, transcript) == "events"


def test_cli_reconcile_reports_changed_and_settling(temp_store, tmp_path, monkeypatch):
    """Two grown transcripts, one quiet and one active. The dry run prints
    both lines and offers --fix. --fix re-ingests only the quiet one."""
    quiet = tmp_path / "quiet.jsonl"
    active = tmp_path / "active.jsonl"
    for path, sid in ((quiet, "cli-quiet"), (active, "cli-active")):
        _append(path, _turn(1, "first", sid))
        _full_ingest(temp_store, path)
        _append(path, _turn(2, f"late reply in {sid}", sid))
    _backdate(quiet, QUIET)
    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda *a, **kw: [quiet, active])

    runner = CliRunner()
    dry = runner.invoke(app, ["reconcile", "--data-dir", str(temp_store.data_dir)])
    assert dry.exit_code == 0, dry.stdout
    assert "1 changed since their last full ingest" in dry.stdout
    assert "1 still being written" in dry.stdout
    assert "Run with --fix" in dry.stdout

    fixed = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(temp_store.data_dir)])
    assert fixed.exit_code == 0, fixed.stdout
    assert "Re-ingested 1" in fixed.stdout
    assert "late reply in cli-quiet" in _assistant_texts(temp_store, "cli-quiet")
    assert "late reply in cli-active" not in _assistant_texts(temp_store, "cli-active")
