#!/usr/bin/env python3
"""recall_diff — snapshot and diff Longhand recall results across code changes.

The pytest suite catches behavior changes against synthetic fixtures. It does
NOT catch ranking changes against the real corpus. This script closes that
gap: it runs a fixed list of queries against the live `~/.longhand/` store
and compares the result shape (top-N episodes, top-N segments, narrative
session IDs) against a saved baseline.

Use it before and after any change to recall_pipeline / narrative / ranking
logic. If the diff is empty the cut is safe to keep. If something shifted,
read the diff and decide whether the change is acceptable or a regression.

Usage:
    # First time: capture a baseline against current code
    python3 scripts/recall_diff.py --save-baseline

    # After making a change: compare against the saved baseline
    python3 scripts/recall_diff.py

    # Use a different query file or store path
    python3 scripts/recall_diff.py --queries scripts/recall_diff_queries.json
    python3 scripts/recall_diff.py --store ~/.longhand
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 8-char lowercase hex (matches the leading slice of a UUIDv4 session_id).
# Strict-hex eliminates non-id words like "Verified" or "material" that
# happen to be 8 chars long.
_SESSION_PREFIX_RE = re.compile(r"\b[0-9a-f]{8}\b")


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _ensure_on_path() -> None:
    sys.path.insert(0, str(_project_root()))


_ensure_on_path()

from longhand.recall.recall_pipeline import recall  # noqa: E402
from longhand.storage.store import LonghandStore  # noqa: E402

DEFAULT_QUERIES_PATH = _project_root() / "scripts" / "recall_diff_queries.json"
DEFAULT_BASELINE_PATH = _project_root() / "scripts" / "recall_diff_baseline.json"
TOP_N = 5


def snapshot(store: LonghandStore, queries: list[str]) -> dict[str, Any]:
    """Run each query against the store and capture a comparable result shape.

    For each query we record:
      - top-N episode IDs with rank, session_id, and `_distance`
      - top-N segment IDs with rank, session_id, and `_distance`
      - the unique session_id 8-char prefixes that appear in the rendered
        narrative (this is what the user actually reads)
    """
    results: dict[str, Any] = {}
    for query in queries:
        result = recall(store, query, max_episodes=TOP_N)
        episodes = [
            {
                "rank": i,
                "episode_id": ep.get("episode_id"),
                "session_id": ep.get("session_id"),
                "distance": round(ep.get("_distance", 1.0), 4),
            }
            for i, ep in enumerate(result.episodes[:TOP_N])
        ]
        segments = [
            {
                "rank": i,
                "segment_id": seg.get("segment_id"),
                "session_id": seg.get("session_id"),
                "distance": round(seg.get("_distance", 1.0), 4),
            }
            for i, seg in enumerate(result.segments[:TOP_N])
        ]
        # Pull the 8-char session prefixes that show up in the narrative
        # text. This is the user-visible surface; if a session disappears
        # from here, the user lost it regardless of internal arrays.
        narrative_session_prefixes = sorted(set(_SESSION_PREFIX_RE.findall(result.narrative)))
        results[query] = {
            "episodes": episodes,
            "segments": segments,
            "narrative_session_prefixes": narrative_session_prefixes,
        }
    return results


def diff(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Compare two snapshots, return one human-readable line per detected change."""
    out: list[str] = []
    all_queries = sorted(set(baseline) | set(current))
    for q in all_queries:
        if q not in baseline:
            out.append(f"NEW QUERY: {q!r}")
            continue
        if q not in current:
            out.append(f"REMOVED QUERY: {q!r}")
            continue
        b, c = baseline[q], current[q]
        # Episodes
        b_eps = [(e["rank"], e["session_id"], e["episode_id"]) for e in b["episodes"]]
        c_eps = [(e["rank"], e["session_id"], e["episode_id"]) for e in c["episodes"]]
        if b_eps != c_eps:
            out.append(f"\n=== {q!r} — EPISODES CHANGED ===")
            out.append(f"  was: {[(r, s) for r, s, _ in b_eps]}")
            out.append(f"  now: {[(r, s) for r, s, _ in c_eps]}")
        # Segments
        b_segs = [(s["rank"], s["session_id"], s["segment_id"]) for s in b["segments"]]
        c_segs = [(s["rank"], s["session_id"], s["segment_id"]) for s in c["segments"]]
        if b_segs != c_segs:
            out.append(f"\n=== {q!r} — SEGMENTS CHANGED ===")
            out.append(f"  was: {[(r, s) for r, s, _ in b_segs]}")
            out.append(f"  now: {[(r, s) for r, s, _ in c_segs]}")
        # Narrative session prefixes (user-visible surface)
        b_nar = b.get("narrative_session_prefixes", [])
        c_nar = c.get("narrative_session_prefixes", [])
        if b_nar != c_nar:
            added = sorted(set(c_nar) - set(b_nar))
            removed = sorted(set(b_nar) - set(c_nar))
            out.append(f"\n=== {q!r} — NARRATIVE SESSIONS CHANGED ===")
            if added:
                out.append(f"  + appeared: {added}")
            if removed:
                out.append(f"  - disappeared: {removed}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--queries",
        type=Path,
        default=DEFAULT_QUERIES_PATH,
        help="JSON file with a list of query strings",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE_PATH,
        help="Path to baseline snapshot file",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="Longhand data dir (default: ~/.longhand/)",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Capture current state as the new baseline (overwrites existing)",
    )
    args = parser.parse_args()

    if not args.queries.exists():
        print(f"queries file not found: {args.queries}", file=sys.stderr)
        return 2
    queries = json.loads(args.queries.read_text())
    if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
        print(f"queries file must be a JSON list of strings: {args.queries}", file=sys.stderr)
        return 2

    store = LonghandStore(data_dir=args.store)

    print(f"running {len(queries)} queries against {store.data_dir} ...")
    current = snapshot(store, queries)

    if args.save_baseline:
        args.baseline.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"baseline saved to {args.baseline}")
        return 0

    if not args.baseline.exists():
        print(
            f"no baseline found at {args.baseline}. "
            "Run with --save-baseline to capture one first.",
            file=sys.stderr,
        )
        return 2
    baseline = json.loads(args.baseline.read_text())

    changes = diff(baseline, current)
    if not changes:
        print("✓ no ranking changes vs baseline")
        return 0
    # Count headers — each query that changed gets one or more "=== query — KIND ===" lines.
    # We also surface NEW/REMOVED query lines.
    changed_queries = {
        line.split("'")[1]
        for line in changes
        if line.startswith("=== '")
    }
    new_or_removed = sum(1 for line in changes if line.startswith(("NEW QUERY:", "REMOVED QUERY:")))
    total = len(changed_queries) + new_or_removed
    print(f"⚠ {total} queries changed:\n")
    for line in changes:
        print(line)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
