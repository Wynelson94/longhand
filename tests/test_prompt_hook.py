"""Regression tests for the UserPromptSubmit auto-context hook (run_prompt_hook).

Guards the bug fixed alongside this file: after the ``cli.py`` -> ``cli/`` package
split, ``run_prompt_hook`` imported ``context`` from ``longhand.cli``, which only
exports ``app``. The resulting ImportError was swallowed by the hook's fail-open
``except``, so the hook emitted ``{}`` on every prompt -- the auto-context feature
was silently dead from v0.9.0 on.

These tests exercise the real stdin -> run_prompt_hook -> context wiring (the exact
surface that broke). The recall engine itself is stubbed for determinism; it is
covered end-to-end by test_episode_pipeline.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from types import SimpleNamespace

from longhand import setup_commands
from longhand.storage.store import LonghandStore


def test_hook_context_import_is_callable():
    """The literal import the hook depends on must resolve to a callable.

    This is the exact contract that broke: ``context`` must be importable from the
    module ``run_prompt_hook`` imports it from, and the package entry point ``app``
    must still be intact.
    """
    from longhand.cli import app
    from longhand.cli._commands import context

    assert callable(context)
    assert app is not None


def _run_hook(monkeypatch, tmp_path, prompt, recall_result):
    """Drive run_prompt_hook with an isolated HOME, a temp store, and a stubbed
    recall result. Returns the hook's stdout, stripped."""
    # Isolate HOME so _load_hook_config() falls back to defaults (enabled,
    # threshold 2.5) instead of reading the developer's real ~/.longhand/config.json.
    monkeypatch.setenv("HOME", str(tmp_path))

    # The hook hardcodes data_dir=None, so redirect store acquisition to a temp store.
    store = LonghandStore(data_dir=tmp_path / "longhand")
    monkeypatch.setattr(
        "longhand.cli._commands._get_store", lambda data_dir=None: store
    )

    # Stub the recall engine. `context` does `from longhand.recall import recall`
    # at call time, so patching the source attribute is sufficient.
    monkeypatch.setattr("longhand.recall.recall", lambda *a, **k: recall_result)

    # Feed the hook its stdin JSON payload.
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        setup_commands.run_prompt_hook()
    return captured.getvalue().strip()


def test_hook_injects_context_when_recall_matches(monkeypatch, tmp_path):
    """A strong recall match must surface as hookSpecificOutput.additionalContext.

    If the cli -> context import regresses, the hook fails open to ``{}`` and this
    assertion fails -- which is exactly the regression being guarded.
    """
    # Episode text literally contains the prompt's keywords, so the context
    # relevance gate (keyword_overlap + confidence*2 >= threshold) clears comfortably.
    episode = {
        "episode_id": "ep_test",
        "session_id": "abcdef1234567890",
        "started_at": "2026-05-28T12:00:00",
        "confidence": 0.5,
        "project_id": None,
        "problem_description": "authentication token refresh middleware was failing",
        "diagnosis_summary": "the session cookie was dropped on cross-origin requests",
        "fix_summary": "set SameSite on the authentication token refresh middleware cookie",
    }
    recall_result = SimpleNamespace(episodes=[episode], artifacts=None)

    prompt = "Why is the authentication token refresh middleware failing again?"
    out = _run_hook(monkeypatch, tmp_path, prompt, recall_result)

    assert out and out != "{}", "hook emitted nothing -- auto-context injection is dead"
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "[Longhand recall" in hso["additionalContext"]
    assert "authentication token refresh middleware" in hso["additionalContext"]


def test_hook_emits_empty_object_when_no_context(monkeypatch, tmp_path):
    """With no relevant episodes the hook must still emit a valid, silent ``{}``."""
    recall_result = SimpleNamespace(episodes=[], artifacts=None)
    out = _run_hook(
        monkeypatch, tmp_path, "some unrelated prompt text here", recall_result
    )
    assert out == "{}"
