"""No test may start a real detached ``longhand`` process.

``project_fallback._spawn_detached`` starts ``python -m longhand <argv>`` in a
new session, so a real child outlives the test run. A background ``ingest``
reads the default transcript folder, ``~/.claude/projects``: on a developer's
machine that is their whole Claude Code history, re-ingested into the test's
temporary store for as long as an hour. Any unmatched project query fires one
(``project_match._fallback_match``) and ten tests reach that path, so every
local run left ten of these behind, and stacked runs can exhaust a machine's
memory. CI never showed it: its runners have no transcripts, so the child
exits at once.

``tests/conftest.py`` refuses that spawn for every test. A test that patches
``subprocess.Popen`` itself still sees the call; a test that needs a real
child opts in with ``@pytest.mark.real_spawn``.
"""

from __future__ import annotations

from longhand.recall import project_fallback


def test_a_test_cannot_start_a_real_background_ingest(temp_store):
    # A fresh store holds no ingest lock, so the only way this returns False
    # is that the spawn itself was refused.
    assert project_fallback.trigger_background_ingest(temp_store) is False
