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
from dataclasses import dataclass
from typing import Literal

Severity = Literal["warning", "error", "fatal"]
Category = Literal["bash", "python", "node", "test", "compile", "http", "generic"]


@dataclass
class ErrorSignal:
    severity: Severity
    category: Category
    snippet: str        # first matching line trimmed
    pattern: str        # which pattern matched (for debugging)


# Patterns are ordered: more specific first.
# Each entry: (regex, severity, category, pattern_name)
_PATTERNS: list[tuple[re.Pattern[str], Severity, Category, str]] = [
    # Python tracebacks — very specific (traceback marker is unambiguous)
    (re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE), "error", "python", "python_traceback"),

    # Node.js specific error classes — must come before generic python exception
    (re.compile(r"^\s*(TypeError|ReferenceError|SyntaxError|RangeError|EvalError):\s", re.MULTILINE), "error", "node", "node_error"),
    (re.compile(r"Cannot find module ['\"]", re.IGNORECASE), "error", "node", "node_module_missing"),
    (re.compile(r"UnhandledPromiseRejection", re.IGNORECASE), "error", "node", "node_unhandled_promise"),

    # Python generic exceptions (after more specific node errors)
    (re.compile(r"^\s*(ValueError|KeyError|IndexError|AttributeError|NameError|ImportError|FileNotFoundError|ZeroDivisionError|\w+Error|\w+Exception):\s", re.MULTILINE), "error", "python", "python_exception"),

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
    (re.compile(r"expected.*?but (was|got|received)", re.IGNORECASE), "error", "test", "expected_got"),
    (re.compile(r"^FAILED\s+", re.MULTILINE), "error", "test", "pytest_failed"),
    (re.compile(r"^\s+\d+ failing"), "error", "test", "mocha_failing"),

    # HTTP errors in bash output
    (re.compile(r"curl:\s*\(\d+\)"), "error", "http", "curl_error"),
    (re.compile(r"HTTP/\d\.\d\s+(4\d\d|5\d\d)"), "error", "http", "http_error_status"),

    # Generic bash errors (least specific, last)
    (re.compile(r"^(panic|fatal):", re.IGNORECASE | re.MULTILINE), "fatal", "bash", "panic_fatal"),
    (re.compile(r"^error:", re.IGNORECASE | re.MULTILINE), "error", "bash", "bash_error_lowercase"),
    (re.compile(r"^Error:", re.MULTILINE), "error", "bash", "bash_error"),
    (re.compile(r"(no such file or directory|permission denied|command not found)", re.IGNORECASE), "error", "bash", "bash_common"),
    (re.compile(r"ENOENT|EACCES|EPERM"), "error", "bash", "bash_errno"),
]


def detect_error(content: str | None) -> ErrorSignal | None:
    """Detect if a tool_result content string indicates an error.

    Returns the first matching ErrorSignal, or None if the content looks clean.
    """
    if not content:
        return None

    text = content if isinstance(content, str) else str(content)
    if not text.strip():
        return None

    # Scan against patterns in priority order
    for pattern, severity, category, name in _PATTERNS:
        match = pattern.search(text)
        if match:
            # Extract the line containing the match
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            if end == -1:
                end = len(text)
            snippet = text[start:end].strip()
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            return ErrorSignal(
                severity=severity,
                category=category,
                snippet=snippet,
                pattern=name,
            )

    return None
