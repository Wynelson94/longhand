"""Canary: probe/search noise must not inflate a session's error count.

Bug class: detect_error runs on raw tool_result text with no idea what
command produced it. A session that probes missing paths (`ls`, `stat` —
ENOENT is the *answer*, not a failure), greps for the literal word
"Error:", and then hits one real pytest failure used to book four errors
instead of one. Session outcomes then skew toward "struggled" and recall
narratives lead with noise.

Audit anchor (2026-07-09): search-command output quoting error words was
the single highest-volume false-positive source on the live corpus.
"""

from __future__ import annotations

from tests.canary_harness import OutputAssertion

DESCRIPTION = (
    "Probe ENOENTs and rg 'Error:' hits are noise; the pytest failure is "
    "the session's only real error — error_detected count must be exactly 1."
)

SESSION_ID = "canary-error-noise-session"
CWD = "/tmp/canary-error-noise"


def _ts(minute: int) -> str:
    return f"2026-07-09T10:{minute:02d}:00.000Z"


def _bash_pair(idx: int, minute: int, command: str, output: str) -> list[dict]:
    """An assistant tool_use (Bash) and its paired user tool_result."""
    return [
        {
            "type": "assistant",
            "uuid": f"a-{idx}",
            "parentUuid": f"u-{idx - 1}" if idx else "u-0",
            "sessionId": SESSION_ID,
            "timestamp": _ts(minute),
            "cwd": CWD,
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_canary_{idx}",
                        "name": "Bash",
                        "input": {"command": command},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": f"r-{idx}",
            "parentUuid": f"a-{idx}",
            "sessionId": SESSION_ID,
            "timestamp": _ts(minute + 1),
            "cwd": CWD,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_canary_{idx}",
                        "content": output,
                    }
                ],
            },
        },
    ]


_EVENTS: list[dict] = [
    {
        "type": "user",
        "uuid": "u-0",
        "parentUuid": None,
        "sessionId": SESSION_ID,
        "timestamp": _ts(0),
        "cwd": CWD,
        "isSidechain": False,
        "message": {"role": "user", "content": "check the config then run the tests"},
    },
    # Probe a maybe-missing path — ENOENT is the answer, not a failure.
    *_bash_pair(
        1, 1, "ls /tmp/definitely-missing", "ls: /tmp/definitely-missing: No such file or directory"
    ),
    # Read-probe an optional config file.
    *_bash_pair(
        2,
        3,
        "cat /etc/canary-missing.conf",
        "cat: /etc/canary-missing.conf: No such file or directory",
    ),
    # Grep for error handling — hits quote error-shaped text verbatim.
    *_bash_pair(
        3,
        5,
        'rg "Error" src/',
        "src/retry.py:7:# retries on AssertionError from the flaky mock\n"
        'src/probe.py:12:msg = "No such file or directory"',
    ),
    # The one real failure in the session.
    *_bash_pair(
        4,
        7,
        "pytest tests/ -q",
        "FAILED tests/test_config.py::test_load - AssertionError: boom\n1 failed, 3 passed in 0.42s",
    ),
]

SESSIONS: list[tuple[str, list[dict]]] = [(f"{SESSION_ID}.jsonl", _EVENTS)]


def _exactly_one_real_error(store) -> tuple[bool, str]:
    with store.sqlite.connect() as conn:
        rows = conn.execute("SELECT error_snippet FROM events WHERE error_detected = 1").fetchall()
    snippets = [r["error_snippet"] for r in rows]
    ok = len(rows) == 1 and "FAILED" in (snippets[0] or "")
    return ok, f"error_detected rows: {len(rows)} — snippets: {snippets}"


ASSERTIONS = [
    OutputAssertion(
        description="only the pytest failure counts as an error",
        predicate=_exactly_one_real_error,
    )
]
