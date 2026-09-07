"""Upstream-drift regression gate for Codex rollouts — the Codex twin of
test_transcript_shapes.py.

Codex Desktop changes its rollout format without notice. Every record shape
we know about must be dispositioned — turned into events, consumed as session
metadata, or skipped by a CODEX_SKIP_* set with a written reason — and this
suite fails the moment a fixture line isn't. Shapes nobody has dispositioned
are preserved as `unknown` events (raw intact) so `longhand doctor` can
surface them, exactly as the Claude parser does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from longhand import codex
from longhand.parser import JSONLParser
from longhand.types import EventType

FIXTURE = Path(__file__).parent / "fixtures" / "codex_shapes" / "entries.jsonl"


def _fixture_entries() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _describe(entry: dict) -> str:
    record_type = entry.get("type")
    kind = (entry.get("payload") or {}).get("type")
    return f"{record_type}/{kind}" if kind else str(record_type)


def test_every_fixture_line_is_dispositioned():
    for entry in _fixture_entries():
        assert codex.disposition(entry) in {"handled", "skipped"}, (
            f"fixture shape {_describe(entry)!r} has no disposition — parse it in "
            "CodexAdapter.convert, or add it to a CODEX_SKIP_* set with a written reason"
        )


def test_fixture_covers_every_skip_set_member():
    entries = _fixture_entries()
    record_types = {e.get("type") for e in entries}
    event_msg_kinds = {
        (e.get("payload") or {}).get("type") for e in entries if e.get("type") == "event_msg"
    }
    missing_records = codex.CODEX_SKIP_RECORD_TYPES - record_types
    assert not missing_records, (
        f"skip-set members without a fixture line: {sorted(missing_records)}"
    )
    missing_kinds = codex.CODEX_SKIP_EVENT_MSG_TYPES - event_msg_kinds
    assert not missing_kinds, (
        f"skipped event_msg kinds without a fixture line: {sorted(missing_kinds)}"
    )


def test_fixture_covers_every_handled_type():
    entries = _fixture_entries()
    record_types = {e.get("type") for e in entries}
    item_kinds = {
        (e.get("payload") or {}).get("type") for e in entries if e.get("type") == "response_item"
    }
    missing_records = codex.CODEX_HANDLED_RECORD_TYPES - record_types
    assert not missing_records, (
        f"handled record types without a fixture line: {sorted(missing_records)}"
    )
    missing_kinds = codex.CODEX_HANDLED_ITEM_TYPES - item_kinds
    assert not missing_kinds, (
        f"handled response_item kinds without a fixture line: {sorted(missing_kinds)}"
    )


def test_skip_and_handled_sets_are_disjoint():
    overlap = codex.CODEX_SKIP_RECORD_TYPES & codex.CODEX_HANDLED_RECORD_TYPES
    assert not overlap, f"a record type cannot be both skipped and handled: {sorted(overlap)}"


def test_fixture_parses_with_expected_dispositions(tmp_path: Path):
    """End-to-end through the real parser: canonical items become events,
    mirrors and bookkeeping produce nothing, nothing lands as unknown."""
    target = tmp_path / "rollout.jsonl"
    target.write_text(FIXTURE.read_text())

    parser = JSONLParser(target)
    events = list(parser.parse_events())

    produced = {e.event_type for e in events}
    assert {
        EventType.USER_MESSAGE,
        EventType.ASSISTANT_TEXT,
        EventType.ASSISTANT_THINKING,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.SYSTEM,
    } <= produced
    assert EventType.UNKNOWN not in produced, "every fixture shape is dispositioned"

    # Skipped mirrors leave no blank rows behind: the only content-free rows
    # are session metadata and injected instructions, which are `system`.
    blank = [e for e in events if not e.content]
    assert blank and all(e.event_type == EventType.SYSTEM for e in blank)

    # The event_msg copy of a message is not a second row; encrypted-only
    # reasoning yields nothing; ciphertext never reaches searchable text.
    assert sum(e.event_type == EventType.USER_MESSAGE for e in events) == 1
    assert sum(e.event_type == EventType.ASSISTANT_TEXT for e in events) == 1
    assert sum(e.event_type == EventType.ASSISTANT_THINKING for e in events) == 1
    assert "OPAQUE_CIPHERTEXT" not in "".join(e.content for e in events)
    assert "SHAPEIMAGE" not in "".join(e.content for e in events)

    session = parser.build_session(events)
    assert session.session_id == "codex:shape-codex-1"
    assert session.model == "gpt-shape"

    # Git signals from a direct shell call and from an `exec` script alike.
    assert {e.git_operation for e in events if e.git_operation} == {"commit", "push"}
    commit = next(e for e in events if e.git_commit_hash)
    assert commit.git_commit_hash == "abc1234"
    push_call = next(e for e in events if e.tool_name == "exec")
    assert push_call.tool_input["command"] == "git push origin main"


def test_unknown_shapes_are_preserved_not_dropped():
    adapter = codex.CodexAdapter({"id": "drift"})
    for entry in (
        {"type": "event_msg", "payload": {"type": "brand_new_kind", "detail": 1}},
        {"type": "response_item", "payload": {"type": "brand_new_item"}},
        {"type": "brand_new_record", "payload": {}},
    ):
        assert codex.disposition(entry) == "unknown"
        (event,) = adapter.convert(entry, 7)
        assert event.event_type == EventType.UNKNOWN
        assert event.raw == entry
        assert event.content, "unknown events carry a searchable stub, like the Claude parser's"


def test_subagent_rollouts_are_recognized(tmp_path: Path):
    primary, guardian = FIXTURE.read_text().splitlines()[:2]
    (tmp_path / "primary.jsonl").write_text(primary + "\n")
    (tmp_path / "guardian.jsonl").write_text(guardian + "\n")
    (tmp_path / "empty.jsonl").write_text("")

    assert codex.is_subagent_rollout(codex.read_session_meta(tmp_path / "primary.jsonl")) is False
    assert codex.is_subagent_rollout(codex.read_session_meta(tmp_path / "guardian.jsonl")) is True
    assert codex.read_session_meta(tmp_path / "empty.jsonl") is None
    assert codex.is_subagent_rollout(None) is False


@pytest.mark.parametrize(
    ("tool_input", "expected"),
    [
        ({"cmd": "git status"}, "git status"),
        ({"cmd": ["git", "status", "--short"]}, "git status --short"),
        ({"command": "ls"}, "ls"),
        (
            {
                "input": 'text(await tools.exec_command({cmd:"git status", max_output_tokens: 500}));'
            },
            "git status",
        ),
        ({"input": "text(await tools.exec_command({cmd: 'pwd'}));"}, "pwd"),
        ({"input": 'text(await tools.exec_command({cmd:"echo \\"hi\\""}));'}, 'echo "hi"'),
        (
            {"input": 'a({cmd:"git add -A"}); b({cmd:"git commit -m x"});'},
            "git add -A\ngit commit -m x",
        ),
        ({"input": 'text(await tools.apply_patch("*** Begin Patch"));'}, ""),
        ({}, ""),
        (None, ""),
    ],
)
def test_shell_commands_from_tool_input(tool_input, expected):
    assert codex.shell_commands_from_tool_input(tool_input) == expected
