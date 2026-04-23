"""
Git operation extraction from Bash tool output.

Deterministic regex-based parser. No LLM, no git binary calls.
Detects: commit, push, pull, checkout, switch, merge, rebase, status, log.

Returns None if the tool call isn't a git command. A GitSignal otherwise.
If a user doesn't have git, this module is never triggered — no impact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass
class GitSignal:
    operation_type: str  # commit, push, pull, checkout, merge, rebase, status, log, diff, tag
    commit_hash: str | None = None
    commit_message: str | None = None
    branch: str | None = None
    remote: str | None = None
    files_changed_count: int | None = None
    success: bool = True


# --- Helpers ---

_GIT_SUBCOMMAND_RE = re.compile(
    r"(?:^|&&\s*|;\s*|\|\|\s*)"  # start of command or chained
    r"(?:sudo\s+)?"  # optional sudo
    r"git\s+"
    r"([a-z][a-z-]*)",  # subcommand
    re.IGNORECASE,
)

# Failure markers in git output
_FAILURE_PATTERNS = [
    re.compile(r"^fatal:", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^error:", re.MULTILINE | re.IGNORECASE),
    re.compile(r"CONFLICT \(", re.MULTILINE),
    re.compile(r"rejected\]", re.MULTILINE),
    re.compile(r"! \[rejected\]", re.MULTILINE),
    re.compile(r"non-fast-forward", re.MULTILINE),
]


def detect_git_command(command: str) -> str | None:
    """Check if a shell command contains a git subcommand.

    Returns the git subcommand (e.g. 'commit', 'push') or None.
    Handles chained commands like 'cd foo && git commit -m "msg"'.
    """
    if not command or "git " not in command.lower():
        return None
    match = _GIT_SUBCOMMAND_RE.search(command)
    return match.group(1).lower() if match else None


def _is_failure(output: str) -> bool:
    return any(p.search(output) for p in _FAILURE_PATTERNS)


# --- Commit ---

_COMMIT_HASH_RE = re.compile(
    r"\[[\w/.:-]+\s+([a-f0-9]{7,})\]"  # [main abc1234] or [feature/foo abc1234...]
)
_COMMIT_MSG_RE = re.compile(
    r"\[[\w/.:-]+\s+[a-f0-9]{7,}\]\s+(.+)"  # message after hash
)
_FILES_CHANGED_RE = re.compile(r"(\d+)\s+files?\s+changed")


def _parse_commit(command: str, output: str) -> GitSignal | None:
    m = _COMMIT_HASH_RE.search(output)
    if not m:
        # Without a parseable commit hash we have nothing worth recording.
        # A "commit" row with no hash ends up rendered as blank in narratives
        # and adds noise to git_operations queries.
        return None
    commit_hash = m.group(1)

    commit_message = None
    files_changed = None

    m = _COMMIT_MSG_RE.search(output)
    if m:
        commit_message = m.group(1).strip()
    m = _FILES_CHANGED_RE.search(output)
    if m:
        files_changed = int(m.group(1))

    # Extract branch from the bracket prefix [main abc123]
    branch = None
    bracket_match = re.search(r"\[([\w/.:-]+)\s+[a-f0-9]", output)
    if bracket_match:
        branch = bracket_match.group(1)

    return GitSignal(
        operation_type="commit",
        commit_hash=commit_hash,
        commit_message=commit_message,
        branch=branch,
        files_changed_count=files_changed,
        success=not _is_failure(output),
    )


# --- Push ---

_PUSH_REMOTE_RE = re.compile(r"To\s+(\S+)")
_PUSH_RANGE_RE = re.compile(r"([a-f0-9]{7,40})\.\.([a-f0-9]{7,40})")
_PUSH_BRANCH_RE = re.compile(r"(\S+)\s+->\s+(\S+)")


def _parse_push(command: str, output: str) -> GitSignal:
    remote = None
    commit_hash = None
    branch = None

    m = _PUSH_REMOTE_RE.search(output)
    if m:
        remote = m.group(1)
    m = _PUSH_RANGE_RE.search(output)
    if m:
        commit_hash = m.group(2)  # the "to" commit
    m = _PUSH_BRANCH_RE.search(output)
    if m:
        branch = m.group(1)

    return GitSignal(
        operation_type="push",
        commit_hash=commit_hash,
        branch=branch,
        remote=remote,
        success=not _is_failure(output),
    )


# --- Checkout / Switch ---

_SWITCHED_RE = re.compile(r"Switched to (?:a new )?branch '([^']+)'")
_CHECKOUT_BRANCH_RE = re.compile(r"Already on '([^']+)'")


def _parse_checkout(command: str, output: str) -> GitSignal:
    branch = None
    m = _SWITCHED_RE.search(output)
    if m:
        branch = m.group(1)
    else:
        m = _CHECKOUT_BRANCH_RE.search(output)
        if m:
            branch = m.group(1)
        else:
            # Try to get branch from command args
            branch_match = re.search(r"git\s+(?:checkout|switch)\s+(?:-[bB]\s+)?(\S+)", command)
            if branch_match:
                val = branch_match.group(1)
                if not val.startswith("-"):
                    branch = val

    return GitSignal(
        operation_type="checkout",
        branch=branch,
        success=not _is_failure(output),
    )


# --- Merge ---

_MERGE_BRANCH_RE = re.compile(r"git\s+merge\s+(\S+)")


def _parse_merge(command: str, output: str) -> GitSignal:
    branch = None
    m = _MERGE_BRANCH_RE.search(command)
    if m:
        branch = m.group(1)

    has_conflict = bool(re.search(r"CONFLICT \(", output))

    commit_hash = None
    m = _COMMIT_HASH_RE.search(output)
    if m:
        commit_hash = m.group(1)

    return GitSignal(
        operation_type="merge",
        commit_hash=commit_hash,
        branch=branch,
        success=not has_conflict and not _is_failure(output),
    )


# --- Status ---

_ON_BRANCH_RE = re.compile(r"On branch (\S+)")
_STATUS_FILE_RE = re.compile(r"^\s+(modified|new file|deleted|renamed):", re.MULTILINE)


def _parse_status(command: str, output: str) -> GitSignal:
    branch = None
    m = _ON_BRANCH_RE.search(output)
    if m:
        branch = m.group(1)

    files_changed = len(_STATUS_FILE_RE.findall(output))

    return GitSignal(
        operation_type="status",
        branch=branch,
        files_changed_count=files_changed if files_changed else None,
        success=True,
    )


# --- Log ---

_LOG_HASH_RE = re.compile(r"^commit ([a-f0-9]{7,})", re.MULTILINE)
_LOG_ONELINE_HASH_RE = re.compile(r"^([a-f0-9]{7,40})\s", re.MULTILINE)


def _parse_log(command: str, output: str) -> GitSignal:
    # Try full commit hashes first
    hashes = _LOG_HASH_RE.findall(output)
    if not hashes:
        hashes = _LOG_ONELINE_HASH_RE.findall(output)

    return GitSignal(
        operation_type="log",
        commit_hash=hashes[0] if hashes else None,
        commit_message=f"{len(hashes)} commits shown" if hashes else None,
        success=True,
    )


# --- Pull ---

_PULL_BRANCH_RE = re.compile(r"From\s+(\S+)")


def _parse_pull(command: str, output: str) -> GitSignal:
    remote = None
    m = _PULL_BRANCH_RE.search(output)
    if m:
        remote = m.group(1)

    commit_hash = None
    m = _PUSH_RANGE_RE.search(output)
    if m:
        commit_hash = m.group(2)

    files_changed = None
    m = _FILES_CHANGED_RE.search(output)
    if m:
        files_changed = int(m.group(1))

    return GitSignal(
        operation_type="pull",
        commit_hash=commit_hash,
        remote=remote,
        files_changed_count=files_changed,
        success=not _is_failure(output),
    )


# --- Diff ---

_DIFF_STAT_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed")


def _parse_diff(command: str, output: str) -> GitSignal:
    files_changed = None
    m = _DIFF_STAT_FILES_RE.search(output)
    if m:
        files_changed = int(m.group(1))

    return GitSignal(
        operation_type="diff",
        files_changed_count=files_changed,
        success=True,
    )


# --- Generic fallback ---

def _parse_generic(subcommand: str, command: str, output: str) -> GitSignal:
    return GitSignal(
        operation_type=subcommand,
        success=not _is_failure(output),
    )


# --- Dispatcher ---

_PARSERS: dict[str, Any] = {
    "commit": _parse_commit,
    "push": _parse_push,
    "pull": _parse_pull,
    "checkout": _parse_checkout,
    "switch": _parse_checkout,
    "merge": _parse_merge,
    "status": _parse_status,
    "log": _parse_log,
    "diff": _parse_diff,
}


def extract_git_signal(command: str, output: str) -> GitSignal | None:
    """Extract structured git data from a Bash command and its output.

    Returns None if the command isn't a git command.
    Returns a GitSignal with whatever data could be parsed otherwise.
    """
    subcommand = detect_git_command(command)
    if not subcommand:
        return None

    parser = _PARSERS.get(subcommand)
    if parser:
        return parser(command, output)

    return _parse_generic(subcommand, command, output)
