"""Tests for the per-session export behind `longhand backup`.

The export is the verbatim record (`events.raw_json`) written back out as
transcript lines: one copy per distinct line, in `get_events` order, masked
in the copy only, split into parts under GitHub's file-size warning, with a
`session.json` beside the parts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from longhand.backup.export import (
    BACKUP_FORMAT,
    PART_BYTES_DEFAULT,
    SessionExport,
    export_session,
    iter_session_lines,
    mask_line,
    read_manifest,
    read_session_json,
    read_session_lines,
    session_rel_dir,
    transcript_base_name,
    write_manifest,
    write_readme_if_absent,
    write_session_files,
    write_session_meta,
)
from longhand.backup.merge import reconcile_session
from longhand.parser import JSONLParser

CODEX_FIXTURE = Path(__file__).parent / "fixtures" / "codex_shapes" / "entries.jsonl"
TOKEN = "ghp_" + "A" * 36


def _ingest(store: Any, path: Path) -> None:
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events, run_analysis=False)


@pytest.fixture
def token_session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sample session's shape with one user message carrying a token-shaped string.

    Redaction at parse time is forced off so the store holds the token verbatim:
    the export must mask the copy, never rely on the store being clean.
    """
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: False)
    path = tmp_path / "token-session.jsonl"
    entries: list[dict[str, Any]] = [
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "sessionId": "token-session-1",
            "timestamp": "2026-05-01T09:00:00.000Z",
            "cwd": "/Users/tester/test-project",
            "gitBranch": "main",
            "isSidechain": False,
            "message": {"role": "user", "content": f"use {TOKEN} for the API call"},
        },
        {
            "type": "assistant",
            "uuid": "asst-1",
            "parentUuid": "user-1",
            "sessionId": "token-session-1",
            "timestamp": "2026-05-01T09:00:01.000Z",
            "cwd": "/Users/tester/test-project",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": "I will not echo that token."}],
            },
        },
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _codex_rollout(tmp_path: Path) -> Path:
    """The Codex shapes fixture as one rollout: its first line is the session_meta record."""
    target = tmp_path / "rollout.jsonl"
    target.write_text(CODEX_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


# ─── Layout ───────────────────────────────────────────────────────────────────


def test_session_rel_dir_buckets_by_start_month():
    row = {"session_id": "abc", "started_at": "2026-04-09T10:00:01+00:00"}
    assert session_rel_dir(row) == "sessions/claude/2026/04/abc"


def test_session_rel_dir_strips_codex_prefix_and_is_filename_safe():
    row = {"session_id": "codex:0199:x/y", "started_at": "2026-09-07T00:00:00+00:00"}
    assert session_rel_dir(row) == "sessions/codex/2026/09/0199_x_y"


def test_session_rel_dir_undated_for_epoch_start():
    row = {"session_id": "abc", "started_at": "1970-01-01T00:00:00+00:00"}
    assert session_rel_dir(row).startswith("sessions/claude/undated/")


def test_session_rel_dir_undated_when_start_missing_or_unparseable():
    assert session_rel_dir({"session_id": "abc"}) == "sessions/claude/undated/abc"
    assert session_rel_dir({"session_id": "abc", "started_at": "yesterday"}).startswith(
        "sessions/claude/undated/"
    )


def test_session_rel_dir_buckets_by_utc_month():
    # 2026-05-01T01:00+03:00 is still April in UTC.
    row = {"session_id": "abc", "started_at": "2026-05-01T01:00:00+03:00"}
    assert session_rel_dir(row) == "sessions/claude/2026/04/abc"


# ─── Lines ────────────────────────────────────────────────────────────────────


def test_iter_session_lines_dedupes_multi_block_lines(temp_store, sample_session_file):
    # the fixture's "asst-1" line yields thinking + text + tool_use events
    _ingest(temp_store, sample_session_file)
    lines = list(iter_session_lines(temp_store.sqlite, "test-session-1"))
    assert sum(1 for line in lines if json.loads(line).get("uuid") == "asst-1") == 1


def test_iter_session_lines_keeps_file_order(temp_store, sample_session_file):
    _ingest(temp_store, sample_session_file)
    exported = [
        json.loads(line) for line in iter_session_lines(temp_store.sqlite, "test-session-1")
    ]
    on_disk = [json.loads(line) for line in sample_session_file.read_text().splitlines()]
    kept = [entry for entry in on_disk if entry in exported]
    # same values, same order, nothing exported that was not in the file
    assert exported == kept
    assert len(exported) == 5


def test_iter_session_lines_unknown_session_is_empty(temp_store):
    assert list(iter_session_lines(temp_store.sqlite, "nope")) == []


# ─── Masking ──────────────────────────────────────────────────────────────────


def test_mask_line_masks_only_when_a_pattern_hits():
    clean = json.dumps({"message": "hello"})
    assert mask_line(clean) == (clean, {})
    masked, counts = mask_line(json.dumps({"message": {"content": TOKEN}}))
    assert TOKEN not in masked
    assert counts == {"github_token": 1}
    assert json.loads(masked)["message"]["content"].endswith("(len=40)")


def test_mask_line_masks_invalid_json_as_text():
    token = "ghp_" + "B" * 36
    masked, counts = mask_line("not json " + token)
    assert token not in masked
    assert counts == {"github_token": 1}


# ─── Parts ────────────────────────────────────────────────────────────────────


def test_write_session_files_single_part_and_sha(tmp_path):
    files = write_session_files(["a", "b"], tmp_path, "transcript")
    assert files.parts == ["transcript.jsonl"]
    assert files.lines == 2
    assert files.bytes == 4
    assert files.sha256 == hashlib.sha256(b"a\nb\n").hexdigest()
    assert (tmp_path / "transcript.jsonl").read_bytes() == b"a\nb\n"


def test_write_session_files_splits_on_line_boundaries(tmp_path):
    files = write_session_files(["x" * 10] * 5, tmp_path, "transcript", part_bytes=25)
    assert files.parts == [
        "transcript.part-0001.jsonl",
        "transcript.part-0002.jsonl",
        "transcript.part-0003.jsonl",
    ]
    joined = b"".join((tmp_path / p).read_bytes() for p in files.parts)
    assert joined == b"".join([b"x" * 10 + b"\n"] * 5)
    assert files.sha256 == hashlib.sha256(joined).hexdigest()


def test_write_session_files_oversized_line_gets_its_own_part(tmp_path):
    files = write_session_files(["a", "y" * 100, "b"], tmp_path, "transcript", part_bytes=10)
    assert len(files.parts) == 3
    assert (tmp_path / files.parts[1]).read_bytes() == b"y" * 100 + b"\n"


def test_write_session_files_removes_stale_parts(tmp_path):
    write_session_files(["x" * 10] * 5, tmp_path, "transcript", part_bytes=25)
    files = write_session_files(["one"], tmp_path, "transcript")
    assert files.parts == ["transcript.jsonl"]
    assert sorted(p.name for p in tmp_path.glob("transcript*.jsonl")) == ["transcript.jsonl"]


def test_write_session_files_utf8_and_lf_only(tmp_path):
    text = "café\n日本\n"
    files = write_session_files(["café", "日本"], tmp_path, "transcript")
    assert (tmp_path / "transcript.jsonl").read_bytes() == text.encode("utf-8")
    assert files.bytes == len(text.encode("utf-8"))


def test_write_session_files_empty_session_writes_one_empty_file(tmp_path):
    files = write_session_files([], tmp_path, "transcript")
    assert files.parts == ["transcript.jsonl"]
    assert files.lines == 0
    assert files.bytes == 0
    assert (tmp_path / "transcript.jsonl").read_bytes() == b""


# ─── Export ───────────────────────────────────────────────────────────────────


def test_export_session_writes_files_and_meta(temp_store, sample_session_file, tmp_path):
    _ingest(temp_store, sample_session_file)
    row = temp_store.sqlite.get_session("test-session-1")
    out = tmp_path / "out"
    exp = export_session(temp_store.sqlite, row, out)
    assert exp.session_id == "test-session-1"
    assert exp.source == "claude"
    assert exp.rel_dir == "sessions/claude/2026/04/test-session-1"
    meta = read_session_json(out / exp.rel_dir)
    assert meta == exp.meta
    assert meta["format"] == BACKUP_FORMAT == 1
    assert meta["longhand_version"]
    assert meta["session_id"] == "test-session-1"
    assert meta["source"] == "claude"
    assert meta["rel_dir"] == exp.rel_dir
    assert meta["parts"] == ["transcript.jsonl"]
    assert meta["lines"] == exp.files.lines == 5
    assert meta["bytes"] == exp.files.bytes > 0
    assert meta["sha256"] == exp.files.sha256
    assert meta["event_count"] == row["event_count"]
    assert meta["last_event_timestamp"].startswith("2026-04-09T10:00:04")
    assert meta["exported_at"].endswith("+00:00")
    assert meta["masked"] is True
    assert meta["original_transcript_path"] == str(sample_session_file)
    assert meta["session"] == row
    assert list(read_session_lines(out / exp.rel_dir, meta)) == list(
        iter_session_lines(temp_store.sqlite, "test-session-1")
    )


def test_export_session_masks_copy_but_not_store(temp_store, tmp_path, token_session_file):
    _ingest(temp_store, token_session_file)
    row = temp_store.sqlite.get_session("token-session-1")
    out = tmp_path / "out"
    exp = export_session(temp_store.sqlite, row, out)
    assert exp.mask_counts == {"github_token": 1}
    for path in out.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8")
    meta = read_session_json(out / exp.rel_dir)
    assert meta["masked"] is True
    first = json.loads(next(iter(read_session_lines(out / exp.rel_dir, meta))))
    assert first["message"]["content"] == "use ghp_\u2026AAAA (len=40) for the API call"
    stored = temp_store.sqlite.get_events(session_id="token-session-1")
    assert any(TOKEN in (event["content"] or "") for event in stored)


def test_export_session_no_mask_keeps_token(temp_store, tmp_path, token_session_file):
    _ingest(temp_store, token_session_file)
    row = temp_store.sqlite.get_session("token-session-1")
    out = tmp_path / "out"
    exp = export_session(temp_store.sqlite, row, out, mask=False)
    assert exp.mask_counts == {}
    meta = read_session_json(out / exp.rel_dir)
    assert meta["masked"] is False
    assert TOKEN in (out / exp.rel_dir / "transcript.jsonl").read_text(encoding="utf-8")


def test_export_session_splits_parts_and_reads_them_back(temp_store, sample_session_file, tmp_path):
    _ingest(temp_store, sample_session_file)
    row = temp_store.sqlite.get_session("test-session-1")
    out = tmp_path / "out"
    exp = export_session(temp_store.sqlite, row, out, part_bytes=400)
    assert len(exp.files.parts) > 1
    assert exp.meta["parts"] == exp.files.parts
    assert list(read_session_lines(out / exp.rel_dir, exp.meta)) == list(
        iter_session_lines(temp_store.sqlite, "test-session-1")
    )


def test_export_codex_session_uses_rollout_base_name(temp_store, tmp_path):
    from longhand.codex import read_session_meta

    _ingest(temp_store, _codex_rollout(tmp_path))
    row = temp_store.sqlite.get_session("codex:shape-codex-1")
    out = tmp_path / "out"
    exp = export_session(temp_store.sqlite, row, out)
    assert exp.source == "codex"
    assert exp.rel_dir == "sessions/codex/2026/04/shape-codex-1"
    assert exp.files.parts == ["rollout.jsonl"]
    first = json.loads(next(iter(read_session_lines(out / exp.rel_dir, exp.meta))))
    assert first["type"] == "session_meta"
    assert read_session_meta(out / exp.rel_dir / "rollout.jsonl")["id"] == "shape-codex-1"


# ─── Repo-level files ─────────────────────────────────────────────────────────


def test_session_meta_round_trip(tmp_path):
    write_session_meta(tmp_path, {"format": 1, "parts": ["transcript.jsonl"]})
    assert read_session_json(tmp_path) == {"format": 1, "parts": ["transcript.jsonl"]}


def test_manifest_round_trip(tmp_path):
    assert read_manifest(tmp_path) is None
    write_manifest(tmp_path, 3)
    manifest = read_manifest(tmp_path)
    assert manifest["sessions"] == 3
    assert manifest["format"] == 1
    assert manifest["longhand_version"]
    assert manifest["updated_at"].endswith("+00:00")


def test_manifest_corrupt_reads_as_none(tmp_path):
    (tmp_path / "manifest.json").write_text("{nope", encoding="utf-8")
    assert read_manifest(tmp_path) is None


def test_readme_written_only_when_absent(tmp_path):
    write_readme_if_absent(tmp_path)
    readme = tmp_path / "README.md"
    assert "longhand restore" in readme.read_text(encoding="utf-8")
    readme.write_text("edited", encoding="utf-8")
    write_readme_if_absent(tmp_path)
    assert readme.read_text(encoding="utf-8") == "edited"


# ─── Never-shrink reconcile against the repo ──────────────────────────────────

REL = "sessions/claude/2026/04/merge-1"


def _stage(
    root: Path,
    rel_dir: str,
    lines: list[str],
    *,
    source: str = "claude",
    part_bytes: int = PART_BYTES_DEFAULT,
) -> SessionExport:
    """A hand-built staged export: parts plus session.json, the shape export_session writes."""
    session_dir = root / Path(rel_dir)
    files = write_session_files(
        lines, session_dir, transcript_base_name(source), part_bytes=part_bytes
    )
    session_id = rel_dir.rsplit("/", 1)[-1]
    meta: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "longhand_version": "test",
        "session_id": session_id,
        "source": source,
        "rel_dir": rel_dir,
        "parts": list(files.parts),
        "lines": files.lines,
        "bytes": files.bytes,
        "sha256": files.sha256,
        "event_count": len(lines),
        "last_event_timestamp": None,
        "exported_at": "2026-10-29T00:00:00+00:00",
        "masked": True,
        "original_transcript_path": None,
        "session": {"session_id": session_id},
    }
    write_session_meta(session_dir, meta)
    return SessionExport(
        session_id=session_id,
        source=source,
        rel_dir=rel_dir,
        files=files,
        mask_counts={},
        meta=meta,
    )


def test_transcript_base_name_by_source():
    assert transcript_base_name("claude") == "transcript"
    assert transcript_base_name("codex") == "rollout"


def test_reconcile_new_moves_staged_into_repo(temp_store, sample_session_file, tmp_path):
    _ingest(temp_store, sample_session_file)
    row = temp_store.sqlite.get_session("test-session-1")
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    exp = export_session(temp_store.sqlite, row, staging)
    result = reconcile_session(exp, staging, repo)
    assert result.outcome == "new"
    assert result.meta == exp.meta
    repo_dir = repo / exp.rel_dir
    assert sorted(p.name for p in repo_dir.iterdir()) == ["session.json", "transcript.jsonl"]
    assert read_session_json(repo_dir) == exp.meta
    assert not (staging / exp.rel_dir).exists()


def test_reconcile_new_replaces_a_partial_repo_dir(tmp_path):
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    partial = repo / Path(REL)
    partial.mkdir(parents=True)
    (partial / "transcript.part-0001.jsonl").write_text("stale\n", encoding="utf-8")
    staged = _stage(staging, REL, ["a", "b"])
    result = reconcile_session(staged, staging, repo)
    assert result.outcome == "new"
    assert sorted(p.name for p in partial.iterdir()) == ["session.json", "transcript.jsonl"]
    assert list(read_session_lines(partial, result.meta)) == ["a", "b"]


def test_reconcile_unchanged_when_sha_matches(temp_store, sample_session_file, tmp_path):
    _ingest(temp_store, sample_session_file)
    row = temp_store.sqlite.get_session("test-session-1")
    repo = tmp_path / "repo"
    first = export_session(temp_store.sqlite, row, tmp_path / "staging-1")
    assert reconcile_session(first, tmp_path / "staging-1", repo).outcome == "new"
    before = (repo / first.rel_dir / "session.json").read_bytes()
    second = export_session(temp_store.sqlite, row, tmp_path / "staging-2")
    result = reconcile_session(second, tmp_path / "staging-2", repo)
    assert result.outcome == "unchanged"
    assert result.meta == first.meta
    assert (repo / first.rel_dir / "session.json").read_bytes() == before
    assert not (tmp_path / "staging-2" / second.rel_dir).exists()


def test_reconcile_subset_leaves_repo_untouched(tmp_path):
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    repo_export = _stage(repo, REL, ["l1", "l2", "l3", "l4", "l5"])
    before = (repo / Path(REL) / "session.json").read_bytes()
    staged = _stage(staging, REL, ["l1", "l2", "l3", "l4"])
    result = reconcile_session(staged, staging, repo)
    assert result.outcome == "subset"
    assert result.meta["sha256"] == repo_export.files.sha256
    assert (repo / Path(REL) / "session.json").read_bytes() == before
    assert list(read_session_lines(repo / Path(REL), result.meta)) == ["l1", "l2", "l3", "l4", "l5"]
    assert not (staging / Path(REL)).exists()


def test_reconcile_subset_with_reordered_lines_is_still_a_subset(tmp_path):
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    _stage(repo, REL, ["a", "b", "c"])
    staged = _stage(staging, REL, ["c", "a"])
    assert reconcile_session(staged, staging, repo).outcome == "subset"


def test_reconcile_grown_writes_union_in_repo_order(tmp_path):
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    _stage(repo, REL, ["a", "b", "c"])
    staged = _stage(staging, REL, ["a", "c", "d"])
    result = reconcile_session(staged, staging, repo)
    assert result.outcome == "grown"
    repo_dir = repo / Path(REL)
    assert list(read_session_lines(repo_dir, result.meta)) == ["a", "b", "c", "d"]
    assert result.meta["lines"] == 4
    assert result.meta["bytes"] == 8
    assert result.meta["sha256"] == hashlib.sha256(b"a\nb\nc\nd\n").hexdigest()
    assert result.meta["parts"] == ["transcript.jsonl"]
    assert result.meta["session_id"] == staged.meta["session_id"]
    assert result.meta["exported_at"] != staged.meta["exported_at"]
    assert read_session_json(repo_dir) == result.meta
    assert sorted(p.name for p in repo_dir.iterdir()) == ["session.json", "transcript.jsonl"]
    assert not (staging / Path(REL)).exists()
    assert not list(staging.rglob("*.merge*"))


def test_reconcile_grown_splits_parts_and_drops_stale_ones(tmp_path):
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    old_lines = [f"x{i}" + "x" * 8 for i in range(3)]  # distinct 10-byte lines
    new_lines = [f"y{i}" + "y" * 8 for i in range(3)]
    _stage(repo, REL, old_lines, part_bytes=25)  # the repo holds two parts
    staged = _stage(staging, REL, new_lines, part_bytes=25)
    result = reconcile_session(staged, staging, repo, part_bytes=25)
    assert result.outcome == "grown"
    repo_dir = repo / Path(REL)
    assert result.meta["parts"] == [f"transcript.part-000{i}.jsonl" for i in (1, 2, 3)]
    assert sorted(p.name for p in repo_dir.glob("*.jsonl")) == result.meta["parts"]
    assert list(read_session_lines(repo_dir, result.meta)) == old_lines + new_lines


def test_reconcile_grown_collapses_duplicate_lines_in_the_staged_copy(tmp_path):
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    _stage(repo, REL, ["a"])
    staged = _stage(staging, REL, ["b", "b", "a"])
    result = reconcile_session(staged, staging, repo)
    assert result.outcome == "grown"
    assert list(read_session_lines(repo / Path(REL), result.meta)) == ["a", "b"]


def test_reconcile_grown_codex_uses_rollout_parts(tmp_path):
    rel = "sessions/codex/2026/09/thread-1"
    staging, repo = tmp_path / "staging", tmp_path / "repo"
    _stage(repo, rel, ["m"], source="codex")
    staged = _stage(staging, rel, ["m", "n"], source="codex")
    result = reconcile_session(staged, staging, repo)
    assert result.outcome == "grown"
    assert result.meta["parts"] == ["rollout.jsonl"]
    assert sorted(p.name for p in (repo / Path(rel)).iterdir()) == ["rollout.jsonl", "session.json"]


# ─── Masking: review fixes C1 / C2 ────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ["line one\n", "\t", "→ ", "value: \r\n"])
def test_mask_line_masks_a_token_after_whitespace_or_non_ascii(prefix: str) -> None:
    line = json.dumps({"type": "user", "message": {"content": f"{prefix}{TOKEN} rest"}})

    masked, counts = mask_line(line)

    assert TOKEN not in masked
    assert counts == {"github_token": 1}
    assert json.loads(masked)["message"]["content"] == f"{prefix}ghp_…AAAA (len=40) rest"


def test_mask_line_masks_a_private_key_block_whole() -> None:
    body = "MIIEow" + "A" * 58 + "\n" + "B" * 64 + "\n"
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}-----END RSA PRIVATE KEY-----"
    line = json.dumps({"type": "tool_result", "content": f"cat id_rsa\n{pem}\ndone"})

    masked, counts = mask_line(line)

    assert "MIIEow" not in masked
    assert "B" * 64 not in masked
    assert counts == {"ssh_private_key": 1}
    content = json.loads(masked)["content"]
    assert content.startswith("cat id_rsa\n")
    assert content.endswith("\ndone")
    assert "PRIVATE KEY" in content


def test_mask_line_masks_a_truncated_private_key_to_the_end_of_the_string() -> None:
    pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "C" * 70 + "\n" + "D" * 70
    line = json.dumps({"content": pem})

    masked, counts = mask_line(line)

    assert "C" * 70 not in masked
    assert "D" * 70 not in masked
    assert counts == {"ssh_private_key": 1}


def test_mask_line_masks_pkcs8_and_encrypted_private_keys() -> None:
    for kind in ("", "ENCRYPTED "):
        pem = (
            f"-----BEGIN {kind}PRIVATE KEY-----\n" + "E" * 64 + f"\n-----END {kind}PRIVATE KEY-----"
        )
        masked, counts = mask_line(json.dumps({"content": pem}))
        assert "E" * 64 not in masked
        assert counts == {"ssh_private_key": 1}


def test_mask_line_invalid_json_masks_private_keys_and_tokens() -> None:
    text = (
        "{not json -----BEGIN EC PRIVATE KEY-----\\nFFFFFFFF\\n-----END EC PRIVATE KEY----- "
        + TOKEN
    )

    masked, counts = mask_line(text)

    assert "FFFFFFFF" not in masked
    assert TOKEN not in masked
    assert counts["ssh_private_key"] == 1
    assert counts["github_token"] == 1


# ─── Review fix I1: interrupted swaps are recovered before anything else ──────


def test_recover_interrupted_restores_a_complete_merge_sidecar(tmp_path: Path) -> None:
    from longhand.backup.merge import recover_interrupted

    live = tmp_path / "sessions" / "claude" / "2026" / "05" / "s-one"
    merge = live.with_name(live.name + ".merge")
    old = live.with_name(live.name + ".old")
    merge.mkdir(parents=True)
    (merge / "transcript.jsonl").write_text('{"a":1}\n')
    (merge / "session.json").write_text(json.dumps({"session_id": "s-one"}))
    old.mkdir()
    (old / "transcript.jsonl").write_text("older\n")

    fixed = recover_interrupted(tmp_path)

    assert (live / "transcript.jsonl").read_text() == '{"a":1}\n'
    assert not merge.exists() and not old.exists()
    assert len(fixed) >= 1


def test_recover_interrupted_drops_an_incomplete_sidecar_and_a_stale_old_dir(
    tmp_path: Path,
) -> None:
    from longhand.backup.merge import recover_interrupted

    live = tmp_path / "sessions" / "claude" / "2026" / "05" / "s-one"
    live.mkdir(parents=True)
    (live / "transcript.jsonl").write_text("live\n")
    (live / "session.json").write_text(json.dumps({"session_id": "s-one"}))
    merge = live.with_name(live.name + ".merge")
    merge.mkdir()
    (merge / "transcript.jsonl").write_text("partial\n")  # no session.json: incomplete
    old = live.with_name(live.name + ".old")
    old.mkdir()
    (old / "transcript.jsonl").write_text("older\n")

    recover_interrupted(tmp_path)

    assert (live / "transcript.jsonl").read_text() == "live\n"
    assert not merge.exists() and not old.exists()


def test_recover_interrupted_restores_the_old_dir_when_nothing_else_is_left(
    tmp_path: Path,
) -> None:
    from longhand.backup.merge import recover_interrupted

    live = tmp_path / "sessions" / "claude" / "2026" / "05" / "s-one"
    old = live.with_name(live.name + ".old")
    old.mkdir(parents=True)
    (old / "transcript.jsonl").write_text("older\n")
    (old / "session.json").write_text(json.dumps({"session_id": "s-one"}))

    recover_interrupted(tmp_path)

    assert (live / "transcript.jsonl").read_text() == "older\n"
    assert not old.exists()


def test_index_repo_sessions_maps_ids_to_dirs_and_shas(tmp_path: Path) -> None:
    from longhand.backup.merge import index_repo_sessions

    a = tmp_path / "sessions" / "claude" / "2026" / "01" / "a"
    b = tmp_path / "sessions" / "codex" / "undated" / "b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "session.json").write_text(json.dumps({"session_id": "a", "sha256": "aa"}))
    (b / "session.json").write_text(json.dumps({"session_id": "codex:b", "sha256": "bb"}))
    (tmp_path / "sessions" / "claude" / "2026" / "01" / "junk").mkdir()

    index = index_repo_sessions(tmp_path)

    assert index == {
        "a": ("sessions/claude/2026/01/a", "aa"),
        "codex:b": ("sessions/codex/undated/b", "bb"),
    }
