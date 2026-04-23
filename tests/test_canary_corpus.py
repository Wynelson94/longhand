"""Auto-discovers and runs every canary fixture under tests/fixtures/corpus/.

Each canary becomes its own pytest test ID — e.g.
`test_canary_corpus.py::test_canary[tests.fixtures.corpus.canary_recall_secondary_match]`

Add a new canary by dropping a `canary_*.py` module under
`tests/fixtures/corpus/` that exposes SESSIONS, ASSERTIONS, and DESCRIPTION.
See tests/fixtures/corpus/README.md for the full convention.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longhand.storage.store import LonghandStore
from tests.canary_harness import discover_canaries, load_canary, write_and_ingest


@pytest.mark.parametrize("canary_module", discover_canaries())
def test_canary(canary_module: str, tmp_path: Path) -> None:
    """Drive one canary fixture end-to-end: ingest its sessions, run its assertions.

    A canary module must expose DESCRIPTION and ASSERTIONS. For the session
    payload it can use either the static `SESSIONS` constant OR a dynamic
    `build_sessions(tmp_path)` factory (when cwd values must be templated to
    the test temp dir, e.g. for project-marker resolution). It can also
    expose an optional `setup(tmp_path)` hook for filesystem prep that runs
    before ingest.
    """
    canary = load_canary(canary_module)

    assert hasattr(canary, "ASSERTIONS"), f"{canary_module} missing ASSERTIONS"
    assert hasattr(canary, "DESCRIPTION"), f"{canary_module} missing DESCRIPTION"

    store = LonghandStore(data_dir=tmp_path / "longhand")
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    if hasattr(canary, "setup"):
        canary.setup(tmp_path)

    if hasattr(canary, "build_sessions"):
        sessions = canary.build_sessions(tmp_path)
    else:
        assert hasattr(canary, "SESSIONS"), (
            f"{canary_module} must expose SESSIONS or build_sessions(tmp_path)"
        )
        sessions = canary.SESSIONS

    write_and_ingest(store, sessions_dir, sessions)

    for assertion in canary.ASSERTIONS:
        assertion.check(store)
