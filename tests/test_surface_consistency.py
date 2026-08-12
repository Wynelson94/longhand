"""Docs must not out-live the surface they describe.

This exists because of a real, shipped regression. PR #69 fixed the README's
Tests badge but missed the closing line of the same file, which still claimed
"316 unit tests passing. All 19 MCP tools stress-tested." — wrong on both
counts, contradicting its own badge two screens above, for two weeks. PR #72
fixed it by hand, along with an undated corpus block and a distribution blurb
claiming 17 MCP tools.

Hand-fixing a count is not a fix; it is the same bug waiting to recur. These
tests assert the docs against the code, so the next drift fails CI instead of
shipping.

Counts are derived, never hardcoded — a test that hardcodes 13 has to be
hand-edited too, which is the very failure mode it is meant to prevent.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

import pytest

from longhand import mcp_server
from longhand.cli import app

REPO = Path(__file__).parent.parent
README = REPO / "README.md"
CLAUDE_MD = REPO / "CLAUDE.md"


def _listed_tools() -> set[str]:
    return {t.name for t in asyncio.run(mcp_server.list_tools())}


def _collected_test_count() -> int:
    """Ask pytest, since parametrized cases are why grepping `def test_` lies."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout
    m = re.search(r"^(\d+) tests collected", out, re.M)
    if not m:
        pytest.skip(f"could not parse collection output: {out[-300:]!r}")
    return int(m.group(1))


# ─── MCP surface ─────────────────────────────────────────────────────────────


def test_readme_mcp_tool_count_matches_the_listing():
    """The count in the docs is the number of tools a user actually sees."""
    listed = len(_listed_tools())
    claims = [int(n) for n in re.findall(r"(\d+)\s+MCP tools", README.read_text())]
    assert claims, "README no longer states an MCP tool count — update this test or the README"
    for claimed in claims:
        assert claimed == listed, (
            f"README claims {claimed} MCP tools; list_tools() returns {listed}"
        )


def test_retired_tools_are_absent_from_the_listing_but_still_dispatch():
    """The two halves of Promise 1 that are easy to break independently."""
    listed = _listed_tools()
    retired = set(mcp_server._RETIRED_TOOLS)
    assert not (listed & retired), f"retired tools leaked back into the listing: {listed & retired}"
    for name in retired:
        assert name in mcp_server._DISPATCH, f"retired tool {name} stopped answering"


def test_claude_md_names_only_real_tools():
    """Backticked tool names in CLAUDE.md must resolve to something callable.

    CLAUDE.md is instructions to an agent — a name that no longer exists sends
    it down a dead path.
    """
    text = CLAUDE_MD.read_text()
    known = _listed_tools() | set(mcp_server._DISPATCH)
    # Only check names we already know are tool-shaped; prose words in
    # backticks are not our business.
    mentioned = {m for m in re.findall(r"`([a-z_]+)`", text) if m in known or m.endswith("_tool")}
    unknown = {m for m in mentioned if m not in known}
    assert not unknown, f"CLAUDE.md names tools that do not exist: {sorted(unknown)}"


# ─── CLI surface ─────────────────────────────────────────────────────────────


def test_docs_do_not_advertise_removed_commands():
    """The 1.0 removals must not survive anywhere in user-facing docs.

    Substring matching would be useless here (`status` contains no removal,
    but `continue` appears inside ordinary prose), so match a command
    invocation shape: `longhand <name>`.
    """
    removed = ["recap", "continue", "patterns", "reanalyze"]
    registered = {
        (info.name or info.callback.__name__.replace("_", "-")) for info in app.registered_commands
    }
    for name in removed:
        assert name not in registered, f"{name} was supposed to be removed at 1.0"

    for doc in (README, CLAUDE_MD):
        text = doc.read_text()
        for name in removed:
            # Allow prose that explains the removal; forbid anything that
            # reads as a live invocation the user could copy.
            for line in text.splitlines():
                if re.search(rf"^\s*longhand {name}\b", line):
                    raise AssertionError(f"{doc.name} still shows `longhand {name}` as usable")


# ─── Measured figures ────────────────────────────────────────────────────────


def test_readme_test_count_matches_collection():
    """Both the badge and the prose, since #69 fixed one and missed the other."""
    actual = _collected_test_count()
    text = README.read_text()

    badge = re.search(r"tests-(\d+)%20passing", text)
    assert badge, "Tests badge missing from README"
    assert int(badge.group(1)) == actual, (
        f"README badge claims {badge.group(1)} tests; pytest collects {actual}"
    )

    for claimed in re.findall(r"(\d+) unit tests passing", text):
        assert int(claimed) == actual, (
            f"README prose claims {claimed} tests; pytest collects {actual}"
        )


def test_corpus_figures_carry_a_measurement_date():
    """A session/event count with no date is a claim that silently rots.

    The Stats section is the one place figures are quoted at scale, so it must
    say when it was measured.
    """
    text = README.read_text()
    stats = text.split("## Stats", 1)
    assert len(stats) == 2, "README lost its Stats section"
    body = stats[1].split("\n## ", 1)[0]

    assert re.search(r"measured \d{4}-\d{2}-\d{2}", body), (
        "Stats block must state the date it was measured"
    )
    # The latency figures come from an older benchmark; they must say so
    # rather than reading as part of the same measurement.
    if "ms median" in body:
        assert "not re-measured" in body or re.search(r"benchmarked on[^\n]*corpus", body), (
            "latency numbers must be attributed to the benchmark that produced them"
        )
