"""Upstream-drift regression gate: the transcript-shapes fixture corpus.

Claude Code adds transcript entry types without notice. Every type we know
about must be explicitly dispositioned — parsed into events, skipped by
`parser.KNOWN_SKIP_ENTRY_TYPES`, or triaged in `TRIAGED_UNKNOWN` with a
written reason — and this suite fails the moment a fixture line isn't.

The loop closes with doctor: unknown-event buildup turns the "Transcript
format" row yellow, `scripts/harvest_entry_shapes.py` harvests the new
shape, the line lands here, and the disposition becomes a reviewed diff
instead of silent unknown-event bloat.
"""

from __future__ import annotations

import json
from pathlib import Path

from longhand import parser as parser_mod
from longhand.parser import JSONLParser
from longhand.types import EventType

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_shapes" / "entries.jsonl"

# Entry types the parser turns into Events.
KNOWN_HANDLED = {"user", "assistant", "system", "file-history-snapshot"}

# The triage registry lives in the parser (single source of truth — doctor's
# drift row excludes dispositioned types too). Adding a member there is a
# decision, not a dodge: every entry needs a fixture line and a written reason.
TRIAGED_UNKNOWN = parser_mod.TRIAGED_UNKNOWN_ENTRY_TYPES


def _fixture_entries() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_every_fixture_type_is_dispositioned():
    known = KNOWN_HANDLED | parser_mod.KNOWN_SKIP_ENTRY_TYPES | TRIAGED_UNKNOWN
    for entry in _fixture_entries():
        entry_type = entry.get("type")
        assert entry_type in known, (
            f"fixture entry type {entry_type!r} has no disposition — parse it, add it "
            "to parser.KNOWN_SKIP_ENTRY_TYPES, or triage it in TRIAGED_UNKNOWN with a reason"
        )


def test_fixture_covers_every_skip_set_member():
    fixture_types = {e.get("type") for e in _fixture_entries()}
    missing = parser_mod.KNOWN_SKIP_ENTRY_TYPES - fixture_types
    assert not missing, f"skip-set members without a fixture line: {sorted(missing)}"


def test_fixture_covers_every_handled_type():
    fixture_types = {e.get("type") for e in _fixture_entries()}
    missing = KNOWN_HANDLED - fixture_types
    assert not missing, f"handled types without a fixture line: {sorted(missing)}"


def test_fixture_covers_every_triaged_type():
    fixture_types = {e.get("type") for e in _fixture_entries()}
    missing = TRIAGED_UNKNOWN - fixture_types
    assert not missing, f"triaged types without a fixture line: {sorted(missing)}"


def test_skip_and_triage_sets_are_disjoint():
    overlap = parser_mod.KNOWN_SKIP_ENTRY_TYPES & TRIAGED_UNKNOWN
    assert not overlap, f"a type cannot be both skipped and preserved: {sorted(overlap)}"


def test_fixture_parses_with_expected_dispositions(tmp_path: Path):
    """End-to-end: handled types produce events, skipped types produce none,
    triaged types land as preserved unknown events."""
    target = tmp_path / "shapes.jsonl"
    target.write_text(FIXTURE.read_text())

    events = list(JSONLParser(target).parse_events())

    produced = {e.event_type for e in events}
    assert EventType.USER_MESSAGE in produced
    assert EventType.ASSISTANT_TEXT in produced
    assert EventType.TOOL_CALL in produced
    assert EventType.TOOL_RESULT in produced
    assert EventType.SYSTEM in produced
    assert EventType.FILE_SNAPSHOT in produced

    # Triaged types are preserved as unknown events — raw intact, visible to
    # doctor — and nothing else leaks into the unknown bucket.
    unknown_types = {e.raw.get("type") for e in events if e.event_type == EventType.UNKNOWN}
    assert unknown_types == set(TRIAGED_UNKNOWN)
