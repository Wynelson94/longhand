"""
On-the-fly project-inference fallback for match-miss.

When `match_projects` returns no hits, we check `~/.claude/projects/` for
session JSONL files that haven't been ingested yet. For each one, we do a
CHEAP project-identity pass: parse the file, run `infer_project`, and
upsert into the projects table. We skip ChromaDB embeddings, episode
extraction, and segment clustering — those are handled by a detached
background `longhand ingest` that this module also fires once.

Rationale:
- Full ingest is 1-2s per session and can balloon to 30s+ when many new
  sessions exist. The user asking "do you remember X" can't wait.
- But we want new projects to be discoverable on the very next query
  without manual `longhand ingest`. Cheap-sync + background-full gets both.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from longhand.analysis.project_inference import infer_project
from longhand.parser import JSONLParser, discover_sessions
from longhand.storage.store import LonghandStore

# Cap to prevent pathological first-run cost. Users with more un-indexed
# sessions than this should run `longhand ingest` manually.
MAX_FALLBACK_FILES = 100


def _lock_path(store: LonghandStore) -> Path:
    return store.data_dir / ".ingest.lock"


def _logs_dir(store: LonghandStore) -> Path:
    return store.data_dir / "logs"


def _lock_holder_alive(pid: int) -> bool:
    """Return True if a process with `pid` is still alive on this system."""
    if pid <= 0:
        return False
    try:
        # Signal 0 just checks existence without delivering a signal.
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        return int(lock_path.read_text().strip())
    except (OSError, ValueError):
        return None


def infer_missing_projects(store: LonghandStore) -> list[dict[str, Any]]:
    """Find un-indexed session JSONLs and infer projects for them (cheap pass).

    Upserts each newly-discovered project into the projects table. Does not
    ingest events, does not embed, does not extract episodes/segments.

    Returns the list of project fingerprints that were upserted.
    """
    files = discover_sessions()
    if not files:
        return []

    # Drop files that are already fully indexed.
    new_files: list[Path] = []
    for file in files:
        try:
            size = file.stat().st_size
        except OSError:
            continue
        if not store.sqlite.already_ingested(str(file), size):
            new_files.append(file)

    if not new_files:
        return []

    # Cap cost. Users with a massive backlog should run `longhand ingest`.
    if len(new_files) > MAX_FALLBACK_FILES:
        new_files = new_files[:MAX_FALLBACK_FILES]

    inferred: list[dict[str, Any]] = []
    for file in new_files:
        try:
            parser = JSONLParser(file)
            events = list(parser.parse_events())
            if not events:
                continue
            session = parser.build_session(events)
            fingerprint = infer_project(session, events)
            store.sqlite.upsert_project(fingerprint)
            inferred.append(fingerprint)
        except Exception:
            # One bad file shouldn't kill the whole fallback.
            continue

    return inferred


def trigger_background_ingest(store: LonghandStore) -> bool:
    """Fire a detached `longhand ingest` in the background.

    Returns True if a new ingest subprocess was spawned; False if one is
    already running (lockfile owned by an alive PID) or we couldn't start.

    The subprocess itself owns the lockfile — see `claim_ingest_lock` in
    this module. This module never writes the lock; it just reads it to
    decide whether to skip spawning.
    """
    lock = _lock_path(store)
    if lock.exists():
        existing_pid = _read_lock_pid(lock)
        if existing_pid and _lock_holder_alive(existing_pid):
            return False
        # Stale — let the child claim/overwrite it.

    logs = _logs_dir(store)
    try:
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return False

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_file = logs / f"background-ingest-{today}.log"

    try:
        # Open inside a `with` so the parent closes its FD as soon as Popen
        # duplicates it into the child. Without this, every fallback-trigger
        # leaks a file descriptor on the calling process.
        with log_file.open("a") as log_fh:
            subprocess.Popen(
                [sys.executable, "-m", "longhand.cli", "ingest"],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except Exception:
        return False


def claim_ingest_lock(store: LonghandStore) -> bool:
    """Try to claim the ingest lockfile for the current process.

    Returns True if the lock is ours (and the caller should proceed with
    the ingest). Returns False if another alive PID already holds the
    lock (the caller should exit without ingesting).

    Safe to call idempotently — if we already hold the lock, returns True.
    """
    lock = _lock_path(store)
    my_pid = os.getpid()

    if lock.exists():
        existing = _read_lock_pid(lock)
        if existing == my_pid:
            return True
        if existing and _lock_holder_alive(existing):
            return False
        # Stale — fall through and overwrite.

    try:
        lock.write_text(str(my_pid))
        return True
    except OSError:
        return False


def release_ingest_lock(store: LonghandStore) -> None:
    """Remove the ingest lockfile if we own it. Safe to call from finally."""
    lock = _lock_path(store)
    if not lock.exists():
        return
    existing = _read_lock_pid(lock)
    if existing != os.getpid():
        return
    try:
        lock.unlink()
    except OSError:
        pass
