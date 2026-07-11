"""
Error detection for tool_result content.

Deterministic regex-based classifier. No LLM. Detects:
- Bash / shell errors (exit codes, stderr markers)
- Python tracebacks and common exception types
- Node.js / JavaScript errors
- Test failures (pytest, jest, vitest, mocha, go test, cargo test)
- Compile errors (TypeScript, Rust, Go, C/C++)
- HTTP errors surfaced through curl/wget

Returns None if no error is detected. An ErrorSignal otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["warning", "error", "fatal"]
Category = Literal["bash", "python", "node", "test", "compile", "http", "generic"]


@dataclass
class ErrorSignal:
    severity: Severity
    category: Category
    snippet: str  # first matching line trimmed
    pattern: str  # which pattern matched (for debugging)


# Patterns are ordered: more specific first.
# Each entry: (regex, severity, category, pattern_name)
_PATTERNS: list[tuple[re.Pattern[str], Severity, Category, str]] = [
    # Python tracebacks — very specific (traceback marker is unambiguous)
    (
        re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE),
        "error",
        "python",
        "python_traceback",
    ),
    # Node.js specific error classes — must come before generic python exception
    (
        re.compile(
            r"^\s*(TypeError|ReferenceError|SyntaxError|RangeError|EvalError):\s", re.MULTILINE
        ),
        "error",
        "node",
        "node_error",
    ),
    (
        re.compile(r"Cannot find module ['\"]", re.IGNORECASE),
        "error",
        "node",
        "node_module_missing",
    ),
    (
        re.compile(r"UnhandledPromiseRejection", re.IGNORECASE),
        "error",
        "node",
        "node_unhandled_promise",
    ),
    # Python generic exceptions (after more specific node errors)
    (
        re.compile(
            r"^\s*(ValueError|KeyError|IndexError|AttributeError|NameError|ImportError|FileNotFoundError|ZeroDivisionError|\w+Error|\w+Exception):\s",
            re.MULTILINE,
        ),
        "error",
        "python",
        "python_exception",
    ),
    # TypeScript compile errors
    (re.compile(r"error TS\d+:"), "error", "compile", "ts_compile_error"),
    (re.compile(r"^Type error:", re.MULTILINE), "error", "compile", "ts_type_error"),
    # Rust compile errors
    (re.compile(r"^error\[E\d+\]:", re.MULTILINE), "error", "compile", "rust_compile_error"),
    (re.compile(r"^error: aborting due to"), "error", "compile", "rust_abort"),
    # Go errors
    (re.compile(r"^.*\.go:\d+:\d+: ", re.MULTILINE), "error", "compile", "go_compile_error"),
    # Test framework failures
    (re.compile(r"^FAIL\s+", re.MULTILINE), "error", "test", "test_fail"),
    (re.compile(r"Tests?:\s+\d+ failed", re.IGNORECASE), "error", "test", "test_summary_fail"),
    (re.compile(r"AssertionError", re.IGNORECASE), "error", "test", "assertion_error"),
    (
        re.compile(r"expected.*?but (was|got|received)", re.IGNORECASE),
        "error",
        "test",
        "expected_got",
    ),
    (re.compile(r"^FAILED\s+", re.MULTILINE), "error", "test", "pytest_failed"),
    (re.compile(r"^\s+\d+ failing"), "error", "test", "mocha_failing"),
    # HTTP errors in bash output
    (re.compile(r"curl:\s*\(\d+\)"), "error", "http", "curl_error"),
    (re.compile(r"HTTP/\d\.\d\s+(4\d\d|5\d\d)"), "error", "http", "http_error_status"),
    # Generic bash errors (least specific, last)
    (re.compile(r"^(panic|fatal):", re.IGNORECASE | re.MULTILINE), "fatal", "bash", "panic_fatal"),
    (re.compile(r"^error:", re.IGNORECASE | re.MULTILINE), "error", "bash", "bash_error_lowercase"),
    (re.compile(r"^Error:", re.MULTILINE), "error", "bash", "bash_error"),
    (
        re.compile(
            r"(no such file or directory|permission denied|command not found)", re.IGNORECASE
        ),
        "error",
        "bash",
        "bash_common",
    ),
    (re.compile(r"ENOENT|EACCES|EPERM"), "error", "bash", "bash_errno"),
]

# Lines that match an error pattern above but are known benign noise.
# Checked against the matched LINE, not the whole output — a benign hit
# skips that one match and keeps scanning, so a real error later in the
# same output still registers.
_BENIGN_LINE_PATTERNS: list[re.Pattern[str]] = [
    # Next.js streaming-SSR / hydration artifacts from dynamic({ssr:false})
    re.compile(r"<!--\s*/?\$!?\s*-->"),
    re.compile(r"data-dgst="),
    # Zero-count test summaries ("0 failing", "Tests: 0 failed")
    re.compile(r"\b0 (?:failing|failed)\b", re.IGNORECASE),
    # Structured payloads reporting an empty/absent error field
    re.compile(r"[\"']?error[\"']?\s*:\s*(?:null|none|\[\]|\{\}|[\"']{2})\s*,?\s*$", re.IGNORECASE),
    # Claude Code Task/TodoWrite tool churn — not a user-code error
    re.compile(r"^Error: Task .* not found|^Error: Task not found", re.IGNORECASE),
    # macOS lacks GNU timeout; harness probes for it constantly
    re.compile(r"command not found: timeout\b|timeout: command not found", re.IGNORECASE),
]


def _is_benign_line(line: str) -> bool:
    return any(p.search(line) for p in _BENIGN_LINE_PATTERNS)


# ─── command-context suppressions ────────────────────────────────────────────
#
# The paired Bash command tells us whether error-shaped output is noise.
# Suppression is per-pattern: a matched rule silences only the listed
# pattern names, everything else keeps scanning, so a real error later in
# the same output still registers (canary_error_false_positives pins this).

_ERRNO_PATTERNS = frozenset({"bash_common", "bash_errno"})

# Pattern classes routinely quoted verbatim in search hits, git history,
# and file contents. Everything except python_traceback — the multi-frame
# traceback header stays live everywhere as the one high-signal structural
# marker.
_ERROR_WORD_PATTERNS = _ERRNO_PATTERNS | frozenset(
    {
        "bash_error",
        "bash_error_lowercase",
        "panic_fatal",
        "python_exception",
        "node_error",
        "node_module_missing",
        "node_unhandled_promise",
        "assertion_error",
        "expected_got",
        "test_fail",
        "test_summary_fail",
        "pytest_failed",
        "mocha_failing",
        "ts_compile_error",
        "ts_type_error",
        "go_compile_error",
        "rust_compile_error",
        "rust_abort",
        "curl_error",
        "http_error_status",
    }
)

_PROBE_COMMANDS = frozenset({"test", "[", "ls", "stat", "which", "command", "type"})
_READ_PROBE_COMMANDS = frozenset({"cat", "head", "tail"})
_SEARCH_COMMANDS = frozenset({"grep", "egrep", "fgrep", "rg", "ag"})
_GIT_READ_SUBCOMMANDS = frozenset(
    {
        "log",
        "show",
        "diff",
        "status",
        "blame",
        "grep",
        "branch",
        "remote",
        "describe",
        "rev-parse",
        "ls-files",
        "shortlog",
        "reflog",
    }
)


def _first_word(command: str) -> str:
    parts = command.strip().split()
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]  # /opt/homebrew/bin/rg → rg


def _is_search(command: str) -> bool:
    return _first_word(command) in _SEARCH_COMMANDS


def _is_git_read(command: str) -> bool:
    parts = command.strip().split()
    return (
        len(parts) >= 2
        and parts[0].rsplit("/", 1)[-1] == "git"
        and parts[1] in _GIT_READ_SUBCOMMANDS
    )


def _is_probe(command: str) -> bool:
    # An explicit 2>/dev/null anywhere is the caller saying "failure is fine".
    return _first_word(command) in _PROBE_COMMANDS or "2>/dev/null" in command


def _is_read_probe(command: str) -> bool:
    return _first_word(command) in _READ_PROBE_COMMANDS


# Ordered — the first matching rule decides which pattern names stay silent
# for this command's output.
_COMMAND_CONTEXT_SUPPRESSIONS: list[tuple[Callable[[str], bool], frozenset[str]]] = [
    # Search output quotes matched lines verbatim — the single highest-volume
    # false-positive source on the live corpus (2026-07-09 audit).
    (_is_search, _ERROR_WORD_PATTERNS),
    # Git history legitimately mentions failures; a live `fatal:` from git
    # itself must still register.
    (_is_git_read, _ERROR_WORD_PATTERNS - {"panic_fatal"}),
    # Existence probes: ENOENT/EACCES *is* the answer, not a failure.
    (_is_probe, _ERRNO_PATTERNS),
    (_is_read_probe, _ERRNO_PATTERNS),
]


def _suppressed_patterns(command: str | None) -> frozenset[str]:
    if not command:
        return frozenset()
    for applies, suppressed in _COMMAND_CONTEXT_SUPPRESSIONS:
        if applies(command):
            return suppressed
    return frozenset()


def detect_error(
    content: str | None,
    *,
    tool_name: str | None = None,
    command: str | None = None,
) -> ErrorSignal | None:
    """Detect if a tool_result content string indicates an error.

    Returns the first matching ErrorSignal, or None if the content looks clean.

    `command` — the paired Bash command, when the caller knows it — drives
    per-pattern suppression via _COMMAND_CONTEXT_SUPPRESSIONS. `tool_name`
    is accepted for callers that have it; no current rule keys on it (the
    parser already gates detection to command-executing tools). Both are
    optional: bare detect_error(content) behaves exactly as before.
    """
    if not content:
        return None

    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        return None

    suppressed = _suppressed_patterns(command)

    # Scan against patterns in priority order
    for pattern, severity, category, name in _PATTERNS:
        if name in suppressed:
            continue
        for match in pattern.finditer(text):
            # Extract the line containing the match
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            if end == -1:
                end = len(text)
            snippet = text[start:end].strip()
            if _is_benign_line(snippet):
                continue
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            return ErrorSignal(
                severity=severity,
                category=category,
                snippet=snippet,
                pattern=name,
            )

    return None
