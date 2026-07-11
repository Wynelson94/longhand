"""Tests for per-event extractors."""

from __future__ import annotations

from longhand.extractors.errors import detect_error
from longhand.extractors.file_refs import extract_file_references
from longhand.extractors.topics import extract_extensions, extract_keywords

# ─── Error detection: command-context suppressions (v0.13) ──────────────────
#
# The paired Bash command tells us whether error-shaped output is noise:
# probing a maybe-missing path, grepping for the word "Error:", or reading
# git history that mentions failures. Suppression is per-pattern — every
# other pattern keeps scanning, so a real error later still registers.

PROBE_ENOENT = "ls: /tmp/definitely-missing: No such file or directory"
TRACEBACK = (
    'Traceback (most recent call last):\n  File "/tmp/app.py", line 42, in main\nValueError: boom'
)


def test_probe_command_suppresses_errno_noise():
    assert (
        detect_error(PROBE_ENOENT, tool_name="Bash", command="ls /tmp/definitely-missing") is None
    )


def test_no_context_backward_compatible():
    # The same content with no command context keeps the old behavior.
    sig = detect_error(PROBE_ENOENT)
    assert sig is not None and sig.pattern == "bash_common"


def test_dev_null_redirect_marks_probe_intent():
    out = "stat: cannot stat '/x': No such file or directory"
    assert detect_error(out, command="stat /x 2>/dev/null || echo absent") is None


def test_read_probe_suppresses_errno():
    out = "cat: /etc/missing.conf: No such file or directory"
    assert detect_error(out, command="cat /etc/missing.conf") is None


def test_search_command_suppresses_error_words():
    # grep-style file:line: prefixes defeat the ^-anchored patterns already;
    # the unanchored ones (AssertionError, ENOENT text) are what false-fire.
    out = (
        "src/retry.py:7:# retries on AssertionError from the flaky mock\n"
        'src/probe.py:12:msg = "No such file or directory"'
    )
    assert detect_error(out, command="rg 'Error' src/") is None
    assert detect_error(out) is not None  # no context → detected, as before


def test_git_read_suppresses_error_words_but_keeps_fatal():
    log_out = (
        "commit abc1234\n"
        "    fix the flaky test\n"
        "\n"
        "    AssertionError: boom was showing up in CI logs\n"
        "    ValueError: now handled gracefully"
    )
    assert detect_error(log_out) is not None  # content alone looks like an error
    assert detect_error(log_out, command="git log") is None

    fatal_out = "fatal: not a git repository (or any of the parent directories): .git"
    sig = detect_error(fatal_out, command="git log")
    assert sig is not None and sig.pattern == "panic_fatal"


def test_real_error_after_suppressed_noise_still_detected():
    out = PROBE_ENOENT + "\n" + TRACEBACK
    sig = detect_error(out, command="ls /tmp/definitely-missing")
    assert sig is not None and sig.pattern == "python_traceback"


def test_non_probe_command_keeps_errno_detection():
    sig = detect_error(PROBE_ENOENT, command="python3 build.py")
    assert sig is not None and sig.pattern == "bash_common"


def test_path_prefixed_search_binary_is_recognized():
    out = "src/retry.py:7:# retries on AssertionError from the flaky mock"
    assert detect_error(out, command="/opt/homebrew/bin/rg 'Error' src/") is None


# ─── Error detection ───────────────────────────────────────────────────────


def test_detect_python_traceback():
    content = """Traceback (most recent call last):
  File "/tmp/app.py", line 42, in main
    result = 1 / 0
ZeroDivisionError: division by zero"""
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "python"
    assert sig.severity == "error"


def test_detect_node_type_error():
    content = "TypeError: Cannot read properties of undefined (reading 'foo')"
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "node"


def test_detect_cannot_find_module():
    content = "Error: Cannot find module 'express'\n    at Function.Module._resolveFilename"
    sig = detect_error(content)
    assert sig is not None
    # Could match node_module_missing OR bash_error - both are valid


def test_detect_typescript_compile_error():
    content = "src/app.ts(42,10): error TS2304: Cannot find name 'foo'."
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "compile"


def test_detect_pytest_failure():
    content = """FAILED tests/test_foo.py::test_bar - AssertionError: expected 3 but got 2
1 failed, 4 passed in 0.23s"""
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "test"


def test_detect_bash_command_not_found():
    content = "bash: foo: command not found"
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "bash"


def test_detect_rust_compile_error():
    content = """error[E0308]: mismatched types
 --> src/main.rs:5:14
  |
5 |     let x: i32 = "hello";
  |              ^^^ expected `i32`, found `&str`"""
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "compile"


def test_clean_output_returns_none():
    content = "All tests passed (15 tests in 0.8s)\nCompiled successfully."
    assert detect_error(content) is None


def test_empty_content_returns_none():
    assert detect_error("") is None
    assert detect_error(None) is None


# ─── File reference extraction ─────────────────────────────────────────────


def test_extract_absolute_path():
    text = "Error at /Users/nate/Projects/game/src/main.ts:42:10"
    refs = extract_file_references(text)
    assert any("main.ts" in r for r in refs)


def test_extract_relative_path():
    text = "See src/components/Button.tsx for the implementation"
    refs = extract_file_references(text)
    assert any("Button.tsx" in r for r in refs)


def test_extract_multiple_paths():
    text = """
    Modified /tmp/a.py and /tmp/b.py
    Also touched src/lib/c.ts
    """
    refs = extract_file_references(text)
    assert len(refs) >= 3


def test_ignores_non_code_paths():
    # .tar.gz and .zip shouldn't match as code files
    text = "Downloaded /tmp/data.tar.gz"
    refs = extract_file_references(text)
    # .gz isn't in our code extensions
    assert not any(r.endswith(".tar.gz") for r in refs)


# ─── Topic extraction ──────────────────────────────────────────────────────


def test_extract_keywords_filters_stopwords():
    texts = [
        "I am building a game with phaser and typescript",
        "The game needs webgl rendering and a state machine",
        "Phaser has a built-in physics engine for games",
    ]
    keywords = extract_keywords(texts, top_k=10)
    assert "phaser" in keywords
    assert "typescript" in keywords or "state" in keywords or "webgl" in keywords
    assert "the" not in keywords
    assert "is" not in keywords


def test_extract_keywords_from_empty():
    assert extract_keywords([]) == []
    assert extract_keywords(["", None]) == []  # type: ignore


def test_extract_extensions():
    paths = [
        "/tmp/a.py",
        "/tmp/b.ts",
        "src/c.tsx",
        "/etc/passwd",
        "Cargo.toml",
    ]
    exts = extract_extensions(paths)
    assert "py" in exts
    assert "ts" in exts
    assert "tsx" in exts
    assert "toml" in exts


def test_benign_nextjs_streaming_markers_not_errors():
    """Next.js dynamic({ssr:false}) streaming-SSR artifacts are benign noise,
    not real errors — they were inflating the unresolved-rate stat."""
    assert detect_error('<!--$!--><template data-dgst="BAILOUT_TO_CSR"></template>') is None
    assert detect_error("<!--/$!-->") is None
    assert detect_error('Error: <template data-dgst="x9"> hydration marker') is None


def test_benign_zero_count_test_summaries_not_errors():
    assert detect_error("  0 failing") is None
    assert detect_error("Tests: 0 failed, 12 passed") is None


def test_benign_empty_error_fields_not_errors():
    assert detect_error("error: null") is None
    assert detect_error('"error": ""') is None


def test_real_error_still_detected_past_benign_noise():
    """A benign line must be skipped, not end the scan — real errors after it
    still register."""
    content = '<!--$!--><template data-dgst="x"></template>\nError: connection refused'
    sig = detect_error(content)
    assert sig is not None
    assert "connection refused" in sig.snippet


def test_nonzero_test_summary_still_detected():
    sig = detect_error("Tests: 3 failed, 9 passed")
    assert sig is not None
    assert sig.pattern == "test_summary_fail"


def test_benign_task_tool_churn_not_error():
    assert detect_error("Error: Task not found") is None
    assert detect_error('Error: Task "3" not found') is None


def test_benign_missing_timeout_binary_not_error():
    assert detect_error("(eval):1: command not found: timeout") is None
    assert detect_error("bash: timeout: command not found") is None


def test_other_command_not_found_still_detected():
    sig = detect_error("(eval):1: command not found: python")
    assert sig is not None
    assert sig.pattern == "bash_common"
