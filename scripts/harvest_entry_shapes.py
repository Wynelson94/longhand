#!/usr/bin/env python3
"""Harvest one example line per transcript entry type from local sessions.

Dev tool for the transcript_shapes fixture corpus. Run it when doctor's
"Transcript format" row goes yellow: it scans ~/.claude/projects for entry
types, prints one sanitized example line per type NOT already covered by
tests/fixtures/transcript_shapes/entries.jsonl, and you review + append the
new lines with a disposition (parse it, add to parser.KNOWN_SKIP_ENTRY_TYPES,
or triage in test_transcript_shapes.TRIAGED_UNKNOWN with a reason).

Sanitization is aggressive — every string is truncated and content bodies
are replaced — because fixture lines get committed. Review before pasting.

Usage: python3 scripts/harvest_entry_shapes.py [--all]
    --all    print an example for every type, not just uncovered ones
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURE = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "transcript_shapes" / "entries.jsonl"
)
_MAX_STR = 60


def _sanitize(value: object, depth: int = 0) -> object:
    if depth > 6:
        return "…"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, dict):
        return {k: _sanitize(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, depth + 1) for v in value[:3]]
    return value


def main() -> int:
    show_all = "--all" in sys.argv

    covered: set[str] = set()
    if FIXTURE.exists() and not show_all:
        for line in FIXTURE.read_text().splitlines():
            if line.strip():
                covered.add(str(json.loads(line).get("type")))

    examples: dict[str, dict] = {}
    projects_dir = Path.home() / ".claude" / "projects"
    for jsonl in sorted(projects_dir.glob("*/*.jsonl")):
        try:
            with jsonl.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    entry_type = str(entry.get("type", "?"))
                    if entry_type in covered or entry_type in examples:
                        continue
                    examples[entry_type] = _sanitize(entry)  # type: ignore[assignment]
        except OSError:
            continue

    if not examples:
        print("# every observed entry type is already covered by the fixture", file=sys.stderr)
        return 0

    for entry_type in sorted(examples):
        print(json.dumps(examples[entry_type], ensure_ascii=False))
    print(
        f"# {len(examples)} uncovered type(s): {', '.join(sorted(examples))} — "
        "sanitize further if needed, then append to the fixture with a disposition",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
