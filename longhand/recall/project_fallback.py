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


# Win32 constants for the liveness probe (values from the Windows SDK).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _win32_pid_alive(pid: int) -> bool:
    """Liveness probe for Windows, where the POSIX signal-0 idiom is lethal.

    On Windows, os.kill(pid, 0) does not probe: CPython maps every non-CTRL
    signal value to TerminateProcess, so the usual "signal 0 existence check"
    kills the lock holder. Query the process handle instead.
    """
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # PID gone, or a process this user cannot query — treat as dead,
        # matching the POSIX branch's PermissionError handling. The lock
        # holder always runs as the same user, so the distinction is moot.
        return False
    try:
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return bool(ok) and exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _lock_holder_alive(pid: int) -> bool:
    """Return True if a process with `pid` is still alive on this system."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win32_pid_alive(pid)
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


def spawn_background(store: LonghandStore, subcommand: list[str], log_prefix: str) -> bool:
    """Fire a detached `longhand <subcommand>` in the background.

    Returns True if a subprocess was spawned; False if an ingest-lock holder
    is already alive (the work is underway — don't stack another process) or
    the spawn failed.

    The subprocess itself owns the lockfile — see `claim_ingest_lock` in
    this module. This function never writes the lock; it just reads it to
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
    log_file = logs / f"{log_prefix}-{today}.log"

    try:
        # Open inside a `with` so the parent closes its FD as soon as Popen
        # duplicates it into the child. Without this, every fallback-trigger
        # leaks a file descriptor on the calling process.
        with log_file.open("a") as log_fh:
            subprocess.Popen(
                # "-m longhand" (the package __main__), NOT "-m longhand.cli":
                # longhand.cli is a package with no __main__.py, so spawning
                # it dies instantly and the background work never runs.
                [sys.executable, "-m", "longhand", *subcommand],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except Exception:
        return False


def trigger_background_ingest(store: LonghandStore) -> bool:
    """Fire a detached `longhand ingest` in the background."""
    return spawn_background(store, ["ingest"], "background-ingest")


def trigger_background_episode_backfill(store: LonghandStore) -> bool:
    """Fire a detached `longhand backfill-episodes` in the background.

    Used by the recall pipeline after an upgrade that added the episodes
    vector collection: embedding the whole corpus inline would block the
    user's prompt (recall runs in the UserPromptSubmit hook), so the work
    happens in a detached process that claims the ingest lock.
    """
    return spawn_background(store, ["backfill-episodes"], "background-backfill")


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
        # Stale — remove it so the atomic create below can claim it.
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False

    try:
        # O_CREAT|O_EXCL makes create-if-absent atomic: if two processes
        # race past the exists() check above, exactly one wins here.
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, str(my_pid).encode())
    finally:
        os.close(fd)
    return True


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
