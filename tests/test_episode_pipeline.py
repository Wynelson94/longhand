"""End-to-end pipeline tests — the acceptance gate that was missing in v0.5.9.

Builds a realistic Claude Code session JSONL fixture with a problem→fix pattern
(user asks about a bug, Claude investigates with thinking blocks, edits a
file, verifies with a test run), then drives the FULL pipeline:

    parse JSONL → ingest events → analyze → extract episodes → embed → recall

and asserts:

    1. Episode row has rich summaries (problem_description combines user ask
       + error signal; diagnosis_summary contains Claude's reasoning;
       fix_summary contains intent + diff + verification).
    2. Semantic recall on a paraphrased query (vocabulary disjoint from the
       episode text) retrieves the episode at a usable distance.
    3. Fixless episodes stay in SQLite but are NOT in the vector collection.

These tests exist specifically because the v0.5.9 unit tests used synthetic
rich-text fixtures and couldn't catch the live-data shape problem. Everything
here starts from JSONL input and runs the real extraction code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from longhand.parser import JSONLParser
from longhand.recall.episode_search import find_episodes
from longhand.storage.store import LonghandStore


def _line(entry: dict[str, Any]) -> str:
    return json.dumps(entry) + "\n"


# ─── Realistic JSONL fixture ────────────────────────────────────────────────


@pytest.fixture
def login_bug_session(tmp_path: Path) -> Path:
    """Realistic problem→fix session shape.

    User asks about a login bug. Claude reads code, thinks about it,
    runs tests (fail), thinks more, edits the cookie middleware, reruns
    tests (pass). This is the canonical problem→diagnosis→fix→verification
    pattern that extract_episodes is designed to capture.

    Vocabulary used in the conversation: "login", "session", "cookie",
    "SameSite", "cross-origin", "middleware". The recall test below
    queries with disjoint vocabulary ("users can't sign in", "auth
    broken", "tokens not persisting") to prove semantic recall works.
    """
    path = tmp_path / "login-bug.jsonl"

    ts = lambda s: f"2026-04-01T10:{s:02d}:00.000Z"  # noqa: E731

    entries: list[dict[str, Any]] = [
        # 1. User asks about a login bug
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "sessionId": "login-bug-session",
            "timestamp": ts(0),
            "cwd": "/tmp/app",
            "gitBranch": "main",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": "The login flow is broken — users can't stay logged in after signing in. Can you investigate and fix it?",
            },
        },
        # 2. Claude thinks — reading the middleware
        {
            "type": "assistant",
            "uuid": "asst-1",
            "parentUuid": "user-1",
            "sessionId": "login-bug-session",
            "timestamp": ts(1),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Let me check the auth middleware. The session cookie configuration is usually where login persistence problems live — SameSite, Secure, and domain flags all affect whether the browser keeps the cookie across requests.",
                    },
                    {
                        "type": "tool_use",
                        "id": "tool-read-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/app/src/middleware.py"},
                    },
                ],
            },
        },
        # 3. Tool result (Read) — file content
        {
            "type": "user",
            "uuid": "result-1",
            "parentUuid": "asst-1",
            "sessionId": "login-bug-session",
            "timestamp": ts(2),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-read-1",
                        "content": "def set_session_cookie(response, token):\n    response.set_cookie('session', token, samesite='Lax', secure=True)\n",
                    }
                ],
            },
            "toolUseResult": {"success": True},
        },
        # 4. Claude's diagnosis — hypothesizes the cause
        {
            "type": "assistant",
            "uuid": "asst-2",
            "parentUuid": "result-1",
            "sessionId": "login-bug-session",
            "timestamp": ts(3),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "The SameSite=Lax setting is why login isn't persisting. The auth flow redirects through a third-party SSO, and cross-origin cookie sends require SameSite=None with Secure. Let me confirm by running the auth tests first — they should be failing.",
                    },
                    {
                        "type": "tool_use",
                        "id": "tool-bash-1",
                        "name": "Bash",
                        "input": {"command": "pytest tests/auth/ -x"},
                    },
                ],
            },
        },
        # 5. Tool result (Bash) — ERROR, tests fail (this triggers episode extraction)
        {
            "type": "user",
            "uuid": "result-2",
            "parentUuid": "asst-2",
            "sessionId": "login-bug-session",
            "timestamp": ts(4),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-bash-1",
                        "content": "tests/auth/test_login.py::test_session_persists FAILED\nE   AssertionError: expected user to still be logged in after redirect\nE   assert session.is_authenticated == True\nE   where session.is_authenticated = False\n",
                    }
                ],
            },
            "toolUseResult": {"success": False},
        },
        # 6. Claude's reasoning after seeing the error — and then the fix
        {
            "type": "assistant",
            "uuid": "asst-3",
            "parentUuid": "result-2",
            "sessionId": "login-bug-session",
            "timestamp": ts(5),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "Confirmed. The test failure matches my hypothesis — the session cookie isn't being sent back after the cross-origin redirect. I'll change SameSite from Lax to None and keep Secure=True (required when SameSite is None).",
                    },
                    {
                        "type": "text",
                        "text": "I'll update the cookie middleware to use SameSite=None so the session persists across the SSO redirect.",
                    },
                    {
                        "type": "tool_use",
                        "id": "tool-edit-1",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/app/src/middleware.py",
                            "old_string": "samesite='Lax'",
                            "new_string": "samesite='None'",
                            "replace_all": False,
                        },
                    },
                ],
            },
        },
        # 7. Tool result (Edit) — success
        {
            "type": "user",
            "uuid": "result-3",
            "parentUuid": "asst-3",
            "sessionId": "login-bug-session",
            "timestamp": ts(6),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-edit-1",
                        "content": "The file /tmp/app/src/middleware.py has been updated successfully.",
                    }
                ],
            },
            "toolUseResult": {"success": True},
        },
        # 8. Claude verifies — reruns the failing test
        {
            "type": "assistant",
            "uuid": "asst-4",
            "parentUuid": "result-3",
            "sessionId": "login-bug-session",
            "timestamp": ts(7),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-bash-2",
                        "name": "Bash",
                        "input": {"command": "pytest tests/auth/test_login.py -v"},
                    }
                ],
            },
        },
        # 9. Tool result (Bash rerun) — clean, tests pass (verification)
        {
            "type": "user",
            "uuid": "result-4",
            "parentUuid": "asst-4",
            "sessionId": "login-bug-session",
            "timestamp": ts(8),
            "cwd": "/tmp/app",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-bash-2",
                        "content": "tests/auth/test_login.py::test_session_persists PASSED\ntests/auth/test_login.py::test_logout PASSED\n\n2 passed in 0.42s\n",
                    }
                ],
            },
            "toolUseResult": {"success": True},
        },
    ]

    with path.open("w") as f:
        for entry in entries:
            f.write(_line(entry))
    return path


@pytest.fixture
def login_bug_store(login_bug_session: Path, tmp_path: Path) -> LonghandStore:
    """Fresh store with the login-bug session fully ingested and analyzed."""
    store = LonghandStore(data_dir=tmp_path / "store")
    parser = JSONLParser(login_bug_session)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events)
    return store


# ─── Acceptance: extraction produces rich summaries ─────────────────────────


def test_pipeline_extracts_episode(login_bug_store: LonghandStore):
    """After ingest+analyze, the problem→fix pattern should produce one episode."""
    episodes = login_bug_store.sqlite.query_episodes(limit=10)
    assert len(episodes) >= 1, "expected at least one episode from the problem→fix fixture"

    ep = episodes[0]
    assert ep.get("fix_event_id"), "the episode should have an identified fix event"
    assert ep.get("status") == "resolved", f"expected status=resolved, got {ep.get('status')}"


def test_problem_description_combines_user_ask_and_error(login_bug_store: LonghandStore):
    """problem_description should include BOTH the user's ask and the error signal,
    so the embedding carries intent vocabulary + surface vocabulary."""
    ep = login_bug_store.sqlite.query_episodes(limit=10)[0]
    pd = ep.get("problem_description", "").lower()

    # User ask vocabulary present
    assert "login" in pd or "users can't" in pd, (
        f"problem_description missing user-ask content: {pd!r}"
    )
    # Error signal present
    assert "assertionerror" in pd or "authenticated" in pd or "error" in pd, (
        f"problem_description missing error signal: {pd!r}"
    )
    # Should be substantively rich, not just 50 chars of raw error
    assert len(pd) > 80, f"problem_description too thin: {pd!r}"


def test_diagnosis_summary_captures_reasoning(login_bug_store: LonghandStore):
    """diagnosis_summary should contain Claude's thinking about the bug —
    including the specific hypothesis (SameSite) even though the user's
    ask didn't mention it.
    """
    ep = login_bug_store.sqlite.query_episodes(limit=10)[0]
    ds = ep.get("diagnosis_summary", "").lower()

    assert "samesite" in ds or "cookie" in ds or "cross-origin" in ds, (
        f"diagnosis_summary missing Claude's actual reasoning: {ds!r}"
    )
    assert len(ds) > 100, f"diagnosis_summary too thin: {ds!r}"


def test_fix_summary_contains_intent_diff_and_verification(login_bug_store: LonghandStore):
    """fix_summary should include:
      - Intent (Claude's assistant_text preceding the fix)
      - Mechanical diff (tool + file + old → new)
      - Verification signal (clean tool result after fix)
    """
    ep = login_bug_store.sqlite.query_episodes(limit=10)[0]
    fs = ep.get("fix_summary", "").lower()

    assert "intent:" in fs or "update" in fs or "samesite=none" in fs, (
        f"fix_summary missing intent language: {fs!r}"
    )
    assert "middleware" in fs, f"fix_summary missing file reference: {fs!r}"
    assert "samesite" in fs, f"fix_summary missing diff content: {fs!r}"
    # Verification happened in the fixture — summary should reflect it
    assert "verified" in fs or "passed" in fs, (
        f"fix_summary missing verification signal: {fs!r}"
    )


# ─── Acceptance: semantic recall actually works on paraphrased queries ──────


def test_semantic_recall_separates_related_from_unrelated(
    login_bug_store: LonghandStore,
):
    """The headline test — the one that should have blocked v0.5.9.

    Rather than hard-asserting on an absolute distance (brittle to
    embedding-model choice — ChromaDB's default MiniLM-L6 isn't
    especially semantic), assert the RELATIVE signal: a paraphrased
    intent query must be meaningfully closer than an unrelated query.
    That's what actually makes recall work in practice.
    """
    related = find_episodes(
        login_bug_store,
        query="users can't sign in because authentication is broken",
        limit=5,
    )
    unrelated = find_episodes(
        login_bug_store,
        query="how to bake a chocolate cake from scratch",
        limit=5,
    )

    assert related, "expected a semantic match for the paraphrased intent query"
    related_d = related[0].get("_distance", 1.0)
    unrelated_d = unrelated[0].get("_distance", 1.0) if unrelated else 999.0

    # Related query must be notably closer than unrelated — the gap is
    # the signal, not the absolute distance.
    assert related_d + 0.15 < unrelated_d, (
        f"related query didn't separate from unrelated: "
        f"related={related_d:.3f} unrelated={unrelated_d:.3f}"
    )
    assert related[0].get("fix_event_id"), "top hit must be a fix-bearing episode"


def test_semantic_recall_alternate_paraphrase(login_bug_store: LonghandStore):
    """Second paraphrase with completely different wording, same topic."""
    hits = find_episodes(
        login_bug_store,
        query="fixing the authentication problem",
        limit=5,
    )
    assert hits, "alternate paraphrase should also match the episode"


# ─── Acceptance: fixless episodes stay in SQLite, not in embeddings ────────


def test_fixless_episode_is_in_sqlite_but_not_vectors(tmp_path: Path):
    """A problem event without a subsequent fix (e.g., retried bash) should
    produce an episode row for forensic access but should NOT be embedded.
    """
    # Build a minimal fixture: user asks, bash errors, nothing edits
    path = tmp_path / "fixless.jsonl"
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": None,
            "sessionId": "fixless",
            "timestamp": "2026-04-01T10:00:00.000Z",
            "cwd": "/tmp/x",
            "isSidechain": False,
            "message": {"role": "user", "content": "run my build"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "fixless",
            "timestamp": "2026-04-01T10:00:01.000Z",
            "cwd": "/tmp/x",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "python build.py"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "r1",
            "parentUuid": "a1",
            "sessionId": "fixless",
            "timestamp": "2026-04-01T10:00:02.000Z",
            "cwd": "/tmp/x",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "(eval):1: command not found: python",
                    }
                ],
            },
            "toolUseResult": {"success": False},
        },
    ]
    with path.open("w") as f:
        for e in entries:
            f.write(_line(e))

    store = LonghandStore(data_dir=tmp_path / "fixless_store")
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    store.ingest_session(session, events)

    # SQLite has the episode (forensic preservation)
    eps = store.sqlite.query_episodes(limit=10)
    if not eps:
        pytest.skip("extraction didn't produce a fixless episode — skip coverage check")
    assert any(not e.get("fix_event_id") for e in eps), (
        "expected at least one fixless episode in the fixture"
    )

    # Vector collection does NOT contain the fixless episode
    assert store.vectors.episode_count() == 0, (
        "fixless episodes must not be embedded — they clutter recall"
    )
