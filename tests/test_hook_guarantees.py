"""Hook-guarantee enforcement suite (v1.0 Promise 3).

Longhand's three Claude Code hooks — SessionEnd (``ingest-session``), Stop
(``ingest-live``), and UserPromptSubmit (``__prompt-hook-run``) — carry three
non-negotiable guarantees:

1. **Never raise**: no failure inside a hook may exit nonzero or propagate an
   exception into Claude Code's hook chain.
2. **Never touch the network**: hooks run on every session end / turn /
   prompt. The CLI's only sanctioned network call is the opt-out update
   check, and hook commands are excluded from it (test_update_check's
   hidden⊆excluded structural test pins that).
3. **Never block the prompt inline**: the UserPromptSubmit hook may spawn
   detached background work, but must never embed corpora, claim the ingest
   lock, or do other heavy work in the user's prompt path.

Per-bucket breadcrumb and exit-code behavior for SessionEnd hook mode lives
in test_ingest_session_stdin.py — this suite enforces the invariants
uniformly across all three hooks and does not duplicate those assertions.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand.cli import app
from longhand.setup_commands import _HOOK_STDIN_MAX_BYTES
from longhand.storage.sqlite_store import SQLiteStore
from longhand.storage.store import LonghandStore

# ─── shared plumbing ──────────────────────────────────────────────────────────


def _session_payload(path: Path) -> str:
    return json.dumps({"transcript_path": str(path), "session_id": "s-guarantee"})


def _episode(episode_id: str = "ep-guarantee") -> dict:
    return {
        "episode_id": episode_id,
        "session_id": "s-guarantee",
        "project_id": "p-guarantee",
        "started_at": "2026-07-01T10:00:00Z",
        "ended_at": "2026-07-01T10:30:00Z",
        "problem_event_id": f"{episode_id}-prob",
        "fix_event_id": f"{episode_id}-fix",
        "problem_description": "authentication token refresh middleware was failing",
        "fix_summary": "set SameSite on the refresh middleware cookie",
        "touched_files": [],
        "tags": [],
        "status": "resolved",
    }


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record-and-raise on every escape hatch to the network.

    Recording matters more than raising: the hooks' own never-raise guards
    would swallow the raise, so the assertion is `attempts == []`, which
    catches even a swallowed phone-home.
    """
    import socket as socket_mod
    import urllib.request as urllib_request

    attempts: list[str] = []
    real_socket = socket_mod.socket

    class _RecordingSocket(real_socket):  # type: ignore[valid-type, misc]
        def __init__(self, *args, **kwargs):
            attempts.append("socket.socket")
            raise RuntimeError("network blocked: socket.socket constructed inside a hook")

    def _blocked(name: str):
        def _fn(*args, **kwargs):
            attempts.append(name)
            raise RuntimeError(f"network blocked: {name} called inside a hook")

        return _fn

    monkeypatch.setattr(socket_mod, "socket", _RecordingSocket)
    monkeypatch.setattr(socket_mod, "create_connection", _blocked("socket.create_connection"))
    monkeypatch.setattr(urllib_request, "urlopen", _blocked("urllib.request.urlopen"))
    return attempts


@pytest.fixture
def warm_embedding_model(tmp_path: Path) -> None:
    """Chroma fetches its local ONNX embedding model on first use — one-time
    installer cost, not hook behavior. Warm it before a no-network window
    opens so the guarantee tests measure the hook, not the installer."""
    store = LonghandStore(data_dir=tmp_path / "warm-model")
    store.sqlite.insert_episodes([_episode("ep-warm")])
    store.backfill_episode_embeddings()


@pytest.fixture
def prompt_hook_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LonghandStore:
    """Isolated HOME + temp store + no real subprocess spawns for the
    UserPromptSubmit hook (it hardcodes data_dir=None)."""
    import subprocess

    monkeypatch.setenv("HOME", str(tmp_path))
    store = LonghandStore(data_dir=tmp_path / "longhand")
    monkeypatch.setattr("longhand.cli._commands._get_store", lambda data_dir=None: store)

    class _DummyProc:
        pid = 424242

    monkeypatch.setattr(
        "longhand.recall.project_fallback.subprocess.Popen",
        lambda *a, **k: _DummyProc(),
        raising=True,
    )
    # Popen is looked up as subprocess.Popen inside project_fallback, which
    # shares the module object — patch both lookups to be explicit.
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _DummyProc())
    return store


PROMPT = "why is the authentication token refresh middleware failing again?"

# ─── guarantee 2: hooks never touch the network ──────────────────────────────


def test_sessionend_hook_full_analysis_is_offline(
    warm_embedding_model: None,
    sample_session_file: Path,
    tmp_path: Path,
    no_network: list[str],
) -> None:
    """A full SessionEnd ingest — parse, analyze, embed — makes zero network
    calls. This is the test that catches a future dependency phoning home."""
    runner = CliRunner()
    data_dir = tmp_path / "lh"

    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(data_dir)],
        input=_session_payload(sample_session_file),
    )

    assert result.exit_code == 0, result.output
    assert no_network == [], f"SessionEnd hook touched the network: {no_network}"

    # The guarantee is not satisfied by failing early — the ingest ran.
    store = LonghandStore(data_dir=data_dir)
    with store.sqlite.connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert n == 1


def test_stop_hook_live_tail_is_offline(
    sample_session_file: Path, tmp_path: Path, no_network: list[str]
) -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["ingest-live", "--data-dir", str(tmp_path / "lh")],
        input=_session_payload(sample_session_file),
    )

    assert result.exit_code == 0, result.output
    assert no_network == [], f"Stop hook touched the network: {no_network}"


def test_prompt_hook_recall_is_offline(
    warm_embedding_model: None,
    prompt_hook_env: LonghandStore,
    no_network: list[str],
) -> None:
    """A real recall pass — SQLite plus vector search — stays offline."""
    prompt_hook_env.sqlite.insert_episodes([_episode()])
    prompt_hook_env.backfill_episode_embeddings()

    runner = CliRunner()
    result = runner.invoke(app, ["__prompt-hook-run"], input=json.dumps({"prompt": PROMPT}))

    assert result.exit_code == 0, result.output
    assert no_network == [], f"UserPromptSubmit hook touched the network: {no_network}"
    json.loads(result.stdout.strip())  # output is valid JSON either way


# ─── guarantee 1: hooks never raise ──────────────────────────────────────────
#
# Each case injects a failure at a different pipeline depth and asserts the
# hook command still exits 0 with no exception escaping. SessionEnd's
# per-bucket breadcrumbs are asserted in test_ingest_session_stdin.py.


def _raise_runtime(*args, **kwargs):
    raise RuntimeError("injected failure")


def _raise_disk_io(*args, **kwargs):
    raise sqlite3.OperationalError("disk I/O error")


SESSION_FAILURES = {
    "store-ctor-raise": lambda mp: mp.setattr(LonghandStore, "__init__", _raise_runtime),
    "store-ctor-disk-io": lambda mp: mp.setattr(LonghandStore, "__init__", _raise_disk_io),
    "parse-raise": lambda mp: mp.setattr(
        "longhand.parser.JSONLParser.parse_events", _raise_runtime
    ),
    "ingest-raise": lambda mp: mp.setattr(LonghandStore, "ingest_session", _raise_runtime),
    "oversize": lambda mp: mp.setattr("longhand.parser.MAX_FILE_SIZE_BYTES", 1),
}


@pytest.mark.parametrize("failure", sorted(SESSION_FAILURES))
def test_sessionend_hook_never_raises(
    failure: str,
    sample_session_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    SESSION_FAILURES[failure](monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ingest-session", "--data-dir", str(tmp_path / "lh")],
        input=_session_payload(sample_session_file),
    )

    assert result.exit_code == 0, f"{failure}: {result.output}"


LIVE_FAILURES = {
    "store-ctor-raise": lambda mp: mp.setattr(LonghandStore, "__init__", _raise_runtime),
    "caught-up-disk-io": lambda mp: mp.setattr(SQLiteStore, "live_caught_up", _raise_disk_io),
    "parse-tail-raise": lambda mp: mp.setattr(
        "longhand.parser.JSONLParser.parse_tail_from_offset", _raise_runtime
    ),
    "insert-raise": lambda mp: mp.setattr(SQLiteStore, "insert_events", _raise_runtime),
    "oversize": lambda mp: mp.setattr("longhand.parser.MAX_FILE_SIZE_BYTES", 1),
}


@pytest.mark.parametrize("failure", sorted(LIVE_FAILURES))
def test_stop_hook_never_raises(
    failure: str,
    sample_session_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    LIVE_FAILURES[failure](monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["ingest-live", "--data-dir", str(tmp_path / "lh")],
        input=_session_payload(sample_session_file),
    )

    assert result.exit_code == 0, f"{failure}: {result.output}"


PROMPT_FAILURES = {
    "recall-raise": lambda mp: mp.setattr("longhand.recall.recall", _raise_runtime),
    "recall-disk-io": lambda mp: mp.setattr("longhand.recall.recall", _raise_disk_io),
    "store-raise": lambda mp: mp.setattr("longhand.cli._commands._get_store", _raise_runtime),
}


@pytest.mark.parametrize("failure", sorted(PROMPT_FAILURES))
def test_prompt_hook_never_raises_and_emits_empty_object(
    failure: str,
    prompt_hook_env: LonghandStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Internal failures must produce the literal fail-open `{}` on stdout."""
    PROMPT_FAILURES[failure](monkeypatch)

    runner = CliRunner()
    result = runner.invoke(app, ["__prompt-hook-run"], input=json.dumps({"prompt": PROMPT}))

    assert result.exit_code == 0, f"{failure}: {result.output}"
    assert result.stdout.strip() == "{}", f"{failure}: {result.stdout!r}"


@pytest.mark.parametrize(
    "raw_stdin",
    ["this is not json at all {{{", "x" * (_HOOK_STDIN_MAX_BYTES + 10)],
    ids=["garbage-stdin", "oversize-stdin"],
)
def test_prompt_hook_survives_hostile_stdin(raw_stdin: str, prompt_hook_env: LonghandStore) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["__prompt-hook-run"], input=raw_stdin)

    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "{}"


# ─── guarantee 3: the prompt hook never blocks the prompt inline ─────────────


def test_prompt_hook_spawns_backfill_instead_of_embedding_inline(
    prompt_hook_env: LonghandStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end through the CLI: with an unembedded corpus the hook may
    only spawn the detached backfill — inline embedding is an outage."""
    store = prompt_hook_env
    store.sqlite.insert_episodes([_episode()])
    assert store.episode_backfill_needed() is True

    def _no_inline(*args, **kwargs):
        raise AssertionError("prompt hook embedded the corpus inline")

    monkeypatch.setattr(store, "backfill_episode_embeddings", _no_inline)
    monkeypatch.setattr(store, "ensure_episode_embeddings", _no_inline)

    spawned: list[int] = []
    monkeypatch.setattr(
        "longhand.recall.recall_pipeline.trigger_background_episode_backfill",
        lambda s: spawned.append(1) or True,
    )

    runner = CliRunner()
    result = runner.invoke(app, ["__prompt-hook-run"], input=json.dumps({"prompt": PROMPT}))

    assert result.exit_code == 0, result.output
    assert spawned == [1]
    assert store.vectors.episode_count() == 0  # nothing embedded inline


def test_prompt_hook_never_claims_ingest_lock(
    prompt_hook_env: LonghandStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ingest lock serializes writers; the prompt path is a reader and
    must never contend for it."""
    from longhand.recall import project_fallback

    store = prompt_hook_env
    store.sqlite.insert_episodes([_episode()])

    claims: list[int] = []
    real_claim = project_fallback.claim_ingest_lock
    monkeypatch.setattr(
        project_fallback,
        "claim_ingest_lock",
        lambda s: claims.append(1) or real_claim(s),
    )

    runner = CliRunner()
    result = runner.invoke(app, ["__prompt-hook-run"], input=json.dumps({"prompt": PROMPT}))

    assert result.exit_code == 0, result.output
    assert claims == [], "the prompt hook claimed the ingest lock"
    assert not (store.data_dir / ".ingest.lock").exists()
