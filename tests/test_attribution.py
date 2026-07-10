"""Project attribution: keep session.cwd and project_id in lockstep, and repair
drift from the events table (the F2 misattribution fix).

These tests avoid touching the real $HOME by simulating a "launched from a
non-project directory" scenario: the noise cwd is a marker-less temp dir (which
_pick_best_project_cwd filters out exactly like it filters $HOME), and the real
work happens in a temp dir that DOES carry a .git marker.
"""

from __future__ import annotations

import json
from pathlib import Path

from longhand.parser import JSONLParser
from longhand.setup_commands import ingest_live_tail
from longhand.storage import LonghandStore


def _write_session_file(path: Path, noise_cwd: str, project_cwd: str) -> Path:
    """A session that starts in `noise_cwd` (no project marker) but does its real
    work — including file edits — in `project_cwd` (which has a .git marker)."""
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "attr-session",
            "timestamp": "2026-04-09T10:00:01.000Z",
            "cwd": noise_cwd,
            "isSidechain": False,
            "message": {"role": "user", "content": "fix the bug in main.py"},
        },
    ]
    # The bulk of the work happens in the real project.
    for i in range(2, 8):
        entries.append(
            {
                "type": "assistant",
                "uuid": f"a{i}",
                "parentUuid": "u1",
                "sessionId": "attr-session",
                "timestamp": f"2026-04-09T10:00:0{i}.000Z",
                "cwd": project_cwd,
                "isSidechain": False,
                "message": {
                    "model": "claude-sonnet-4-6",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"t{i}",
                            "name": "Edit",
                            "input": {
                                "file_path": f"{project_cwd}/main.py",
                                "old_string": f"old{i}",
                                "new_string": f"new{i}",
                            },
                        }
                    ],
                },
            }
        )
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def _make_project_dir(tmp_path: Path, name: str) -> str:
    proj = tmp_path / name
    (proj / ".git").mkdir(parents=True)
    return str(proj)


def test_build_session_prefers_real_project_over_launch_dir(tmp_path):
    """Sanity: build_session already picks the real project cwd, not the noise
    launch dir. (Guards the signal the attribution fix depends on.)"""
    project_cwd = _make_project_dir(tmp_path, "realproj")
    noise_cwd = str(tmp_path / "nowhere")
    Path(noise_cwd).mkdir()
    sf = _write_session_file(tmp_path / "s.jsonl", noise_cwd, project_cwd)

    parser = JSONLParser(sf)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    assert session.cwd == project_cwd


def test_attribute_session_project_attaches_and_recomputes(tmp_path):
    """attribute_session_project infers + attaches the project and refreshes the
    project rollups, keeping cwd↔project_id in lockstep — without running the
    heavy episode/segment analysis."""
    project_cwd = _make_project_dir(tmp_path, "realproj")
    noise_cwd = str(tmp_path / "nowhere")
    Path(noise_cwd).mkdir()
    sf = _write_session_file(tmp_path / "s.jsonl", noise_cwd, project_cwd)

    store = LonghandStore(data_dir=tmp_path / "lh")
    parser = JSONLParser(sf)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)  # events only, no attribution

    project = store.attribute_session_project(session, events)

    assert project["canonical_path"] == project_cwd
    row = store.sqlite.get_session(session.session_id)
    assert row["project_id"] == project["project_id"]
    p = store.sqlite.get_project(project["project_id"])
    assert p["session_count"] == 1
    assert p["total_edits"] == session.file_edit_count


def test_reattribute_repairs_drift_from_events_table(tmp_path):
    """The backfill re-derives the project from events in the DB (NOT the
    transcript) and repairs a session whose project_id has drifted to the wrong
    project — even when its stored cwd has drifted too."""
    project_cwd = _make_project_dir(tmp_path, "realproj")
    other_cwd = _make_project_dir(tmp_path, "wrongproj")
    noise_cwd = str(tmp_path / "nowhere")
    Path(noise_cwd).mkdir()
    sf = _write_session_file(tmp_path / "s.jsonl", noise_cwd, project_cwd)

    store = LonghandStore(data_dir=tmp_path / "lh")
    parser = JSONLParser(sf)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=True)

    real_pid = store.sqlite.get_session(session.session_id)["project_id"]
    assert store.sqlite.get_project(real_pid)["canonical_path"] == project_cwd

    # Simulate desync: drift both project_id and cwd to the wrong/noise values,
    # mimicking a no-analysis re-ingest + MAX(cwd) overwrite.
    from longhand.analysis.project_inference import _canonicalize_path, _project_id_for

    wrong_pid = _project_id_for(_canonicalize_path(other_cwd))
    with store.sqlite.connect() as conn:
        conn.execute(
            "UPDATE sessions SET project_id = ?, cwd = ? WHERE session_id = ?",
            (wrong_pid, noise_cwd, session.session_id),
        )
        conn.commit()

    result = store.reattribute_sessions()

    assert result["reattributed"] >= 1
    fixed = store.sqlite.get_session(session.session_id)
    assert fixed["project_id"] == real_pid  # back to the real project
    assert fixed["cwd"] == project_cwd  # cwd re-derived from events too


def test_live_tail_sets_cwd_to_project_not_lexicographic_max(tmp_path):
    """The Stop-hook live tail must derive cwd from the real project (like
    build_session), not MAX(cwd) — a lexicographically larger, marker-less dir
    would otherwise win and desync cwd from project_id."""
    proj = _make_project_dir(tmp_path, "proj")  # has a .git marker
    noise = str(tmp_path / "zzz_noise")  # no marker, lexicographically AFTER proj
    Path(noise).mkdir()
    assert noise > proj  # sanity: MAX(cwd) would pick the noise dir

    transcript = tmp_path / "live.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "live-attr",
            "timestamp": "2026-04-28T10:00:00.000Z",
            "cwd": noise,
            "isSidechain": False,
            "message": {"role": "user", "content": "start"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "live-attr",
            "timestamp": "2026-04-28T10:00:01.000Z",
            "cwd": proj,
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {
                            "file_path": f"{proj}/main.py",
                            "old_string": "a",
                            "new_string": "b",
                        },
                    }
                ],
            },
        },
    ]
    with transcript.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    data_dir = tmp_path / "lh"
    ingest_live_tail(str(transcript), str(data_dir))

    store = LonghandStore(data_dir=data_dir)
    row = store.sqlite.get_session("live-attr")
    assert row is not None
    assert row["cwd"] == proj


# ─── v0.12 attribution overhaul ──────────────────────────────────────────────


def _mini_session(cwd: str | None, tmp_path: Path):
    """Session + events with a given cwd, without touching disk transcripts."""
    from datetime import datetime, timezone

    from longhand.types import Session

    return Session(
        session_id="s-attr",
        project_path=cwd or "",
        transcript_path=str(tmp_path / "t.jsonl"),
        started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        event_count=1,
        user_message_count=1,
        assistant_message_count=0,
        tool_call_count=0,
        file_edit_count=0,
        git_branch=None,
        cwd=cwd,
        model=None,
    )


def test_monorepo_package_attributes_to_git_root(tmp_path):
    """repo/.git outranks repo/apps/web/package.json — sessions in the package
    dir must not split into a separate per-package project."""
    from longhand.analysis.project_inference import infer_project

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    web = repo / "apps" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text("{}")

    at_root = infer_project(_mini_session(str(repo), tmp_path), [])
    in_package = infer_project(_mini_session(str(web), tmp_path), [])
    assert in_package["canonical_path"] == str(repo)
    assert in_package["project_id"] == at_root["project_id"]


def test_nested_git_collapses_to_outermost(tmp_path):
    from longhand.analysis.project_inference import infer_project

    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    inner = outer / "vendor" / "lib"
    inner.mkdir(parents=True)
    (inner / ".git").mkdir()

    fp = infer_project(_mini_session(str(inner), tmp_path), [])
    assert fp["canonical_path"] == str(outer)


def test_nearest_non_git_marker_still_wins_without_git(tmp_path):
    from longhand.analysis.project_inference import infer_project

    pkg = tmp_path / "plain" / "pkg"
    sub = pkg / "src"
    sub.mkdir(parents=True)
    (pkg / "package.json").write_text("{}")

    fp = infer_project(_mini_session(str(sub), tmp_path), [])
    assert fp["canonical_path"] == str(pkg)


def test_case_dupe_spellings_mint_one_project(tmp_path):
    """~/Projects/Foo and ~/projects/foo are the same dir on a case-insensitive
    filesystem — both spellings must hash to one project_id."""
    import pytest

    from longhand.analysis.project_inference import infer_project

    proj = tmp_path / "CamelCase"
    (proj / ".git").mkdir(parents=True)
    alt = Path(str(proj).replace("CamelCase", "camelcase"))
    if not alt.exists():
        pytest.skip("case-sensitive filesystem — dupe spellings are distinct dirs")

    a = infer_project(_mini_session(str(proj), tmp_path), [])
    b = infer_project(_mini_session(str(alt), tmp_path), [])
    assert a["project_id"] == b["project_id"]
    assert a["canonical_path"] == b["canonical_path"] == str(proj)


def test_reserved_paths_go_to_unattributed_bucket(tmp_path):
    from longhand.analysis.project_inference import (
        UNATTRIBUTED_PROJECT_ID,
        infer_project,
    )

    home = str(Path.home())
    claude_internal = str(Path.home() / ".claude" / "projects" / "some-slug")
    markerless_pytest_dir = str(tmp_path)  # under pytest-of-*, no markers
    tmp_scratch = "/tmp/scratch-abc123"

    for cwd in (home, claude_internal, markerless_pytest_dir, tmp_scratch, None):
        fp = infer_project(_mini_session(cwd, tmp_path), [])
        assert fp["project_id"] == UNATTRIBUTED_PROJECT_ID, cwd
        assert fp["display_name"] == "unattributed"
        assert fp["keywords"] == [] and fp["aliases"] == ["unattributed"]


def test_markered_dir_under_tmp_is_a_real_project(tmp_path):
    """The reserved-path guard only applies to MARKERLESS paths — a real repo
    checked out under a temp dir still attributes normally."""
    from longhand.analysis.project_inference import (
        UNATTRIBUTED_PROJECT_ID,
        infer_project,
    )

    repo = tmp_path / "real-work"
    (repo / ".git").mkdir(parents=True)

    fp = infer_project(_mini_session(str(repo), tmp_path), [])
    assert fp["project_id"] != UNATTRIBUTED_PROJECT_ID
    assert fp["canonical_path"] == str(repo)


def test_reattribute_dry_run_reports_without_writing(tmp_path):
    """Dry-run (the new default) lists the moves and touches nothing; --fix
    applies them and prunes the emptied project row."""
    store = LonghandStore(data_dir=tmp_path / "lh")

    repo = tmp_path / "realproj"
    (repo / ".git").mkdir(parents=True)
    session_file = _write_session_file(tmp_path / "s.jsonl", str(tmp_path / "noise"), str(repo))
    parser = JSONLParser(session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=True)

    # Simulate legacy drift: force the session onto a fake junk project.
    store.sqlite.upsert_project(
        {
            "project_id": "p_junk",
            "canonical_path": "/tmp/junk",
            "display_name": "junk",
            "aliases": ["junk"],
            "keywords": [],
            "languages": [],
            "category": None,
            "first_seen": "2026-07-01T00:00:00+00:00",
            "last_seen": "2026-07-01T00:00:00+00:00",
        }
    )
    with store.sqlite.connect() as conn:
        conn.execute(
            "UPDATE sessions SET project_id = 'p_junk' WHERE session_id = ?",
            (session.session_id,),
        )
    store.sqlite.recompute_project_stats("p_junk")

    dry = store.reattribute_sessions(apply=False)
    assert dry["applied"] is False
    assert dry["reattributed"] == 1
    assert dry["changes"][0]["old_project_id"] == "p_junk"
    assert dry["pruned"] == 1  # p_junk would be emptied
    with store.sqlite.connect() as conn:
        still = conn.execute(
            "SELECT project_id FROM sessions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
    assert still == "p_junk"  # nothing written

    fixed = store.reattribute_sessions(apply=True)
    assert fixed["applied"] is True and fixed["reattributed"] == 1
    with store.sqlite.connect() as conn:
        moved = conn.execute(
            "SELECT project_id FROM sessions WHERE session_id = ?", (session.session_id,)
        ).fetchone()[0]
    assert moved != "p_junk"
    assert store.sqlite.get_project("p_junk") is None  # pruned
