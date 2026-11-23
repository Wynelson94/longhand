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

import contextlib
import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

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
    # This runs inside the UserPromptSubmit hook: never claim, never wait.
    # When a live ingest holds the lock, the projects it is building will be
    # visible on the next query anyway — defer rather than stack writers.
    lock = _lock_path(store)
    if lock.exists():
        holder = _read_lock_pid(lock)
        if holder and holder != os.getpid() and _lock_holder_alive(holder):
            return []

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


def _spawn_detached(
    data_dir: Path, argv: list[str], log_prefix: str
) -> subprocess.Popen[bytes] | None:
    """Start a detached `longhand <argv>` logging to logs/<prefix>-<date>.log.

    The child starts in the data directory, with LONGHAND_DATA_DIR set to it.
    `python -m` imports from the working directory first, and hooks run in
    the session's project directory: inheriting that would run a `longhand/`
    package sitting there (a session inside a Longhand checkout) instead of
    the installed one. A source checkout run without installing therefore
    needs PYTHONPATH for its children too.

    Returns the process, or None when the log directory or the spawn failed.
    Never raises.
    """
    try:
        data_dir = data_dir.absolute()
        logs_dir = data_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = logs_dir / f"{log_prefix}-{today}.log"

        # Open inside a `with` so the parent closes its FD as soon as Popen
        # duplicates it into the child. Without this, every fallback-trigger
        # leaks a file descriptor on the calling process.
        with log_file.open("a") as log_fh:
            return subprocess.Popen(
                # "-m longhand" (the package __main__), NOT "-m longhand.cli":
                # longhand.cli is a package with no __main__.py, so spawning
                # it dies instantly and the background work never runs.
                [sys.executable, "-m", "longhand", *argv],
                cwd=str(data_dir),
                env={**os.environ, "LONGHAND_DATA_DIR": str(data_dir)},
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
    except Exception:
        return None


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

    return _spawn_detached(store.data_dir, subcommand, log_prefix) is not None


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


# ─── Background retry for a hook that found the ingest lock busy ───────────

# How long a retry waits for the lock before it gives up, leaving a
# lock-timeout breadcrumb and the transcript for reconcile.
RETRY_WAIT_SECONDS = 600

RetryCommand = Literal["ingest-live", "ingest-session"]
RetryOutcome = Literal["spawned", "waiting", "failed"]

# Hook command → its marker directory, which is also its daily log's prefix.
_RETRY_KINDS: dict[str, str] = {"ingest-live": "live-retry", "ingest-session": "session-retry"}

# A waiter clears its marker as soon as it holds the lock, so a live marker is
# never much older than the wait. An older one outlived a killed waiter whose
# PID the OS may since have handed to an unrelated process.
_RETRY_MARKER_MAX_AGE_SECONDS = RETRY_WAIT_SECONDS + 120


def _retry_marker(data_dir: Path, command: RetryCommand, transcript: Path) -> Path:
    """The per-transcript marker that holds the PID of its retry waiter."""
    digest = hashlib.sha256(os.fsencode(str(transcript))).hexdigest()[:16]
    return data_dir / _RETRY_KINDS[command] / f"{digest}.pid"


def _marker_is_live(marker: Path) -> bool:
    """A waiter still owns `marker`: its PID is alive and it is young enough."""
    pid = _read_lock_pid(marker)
    if not pid or not _lock_holder_alive(pid):
        return False
    try:
        age = time.time() - marker.stat().st_mtime
    except OSError:
        return False
    return age <= _RETRY_MARKER_MAX_AGE_SECONDS


def _claim_retry_marker(marker: Path) -> bool:
    """Create `marker` holding our PID. False when a live waiter owns it.

    O_EXCL makes the create atomic between hooks racing on one transcript.
    A marker whose waiter is gone (dead PID, empty, or older than any wait)
    is replaced.
    """
    for _ in range(2):
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _marker_is_live(marker):
                return False
            with contextlib.suppress(FileNotFoundError):
                marker.unlink()
            continue
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return True
    return False


def spawn_ingest_retry(data_dir: Path, command: RetryCommand, transcript: Path) -> RetryOutcome:
    """Hand a hook's locked-out ingest to a detached waiter process.

    The Stop and SessionEnd hooks never block on the ingest lock. When
    another ingest holds it they call this, rather than defer to a later
    hook that may never come (the session may be over). The waiter runs
    `longhand <command> --transcript <path> --wait-lock RETRY_WAIT_SECONDS`
    and writes one line per retry to logs/<kind>-<date>.log, so lock-outs
    can be counted.

    One waiter per transcript and command, named by a marker holding its
    PID. A live waiter reads the transcript to its end once it has the
    lock, so it covers this lock-out too. Returns "spawned", "waiting" (a
    live waiter already exists) or "failed". Never raises.
    """
    try:
        # Absolute once, here: the waiter starts in the data dir, and it finds
        # this marker again by hashing the same transcript path string.
        data_dir = Path(data_dir).absolute()
        transcript = Path(transcript).absolute()
        marker = _retry_marker(data_dir, command, transcript)
        marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _claim_retry_marker(marker):
            return "waiting"
        argv = [
            command,
            "--transcript",
            str(transcript),
            "--wait-lock",
            str(RETRY_WAIT_SECONDS),
            "--data-dir",
            str(data_dir),
        ]
        proc = _spawn_detached(data_dir, argv, _RETRY_KINDS[command])
        if proc is None:
            with contextlib.suppress(OSError):
                marker.unlink()
            return "failed"
        # Our PID stood in while we spawned; the marker now names the waiter.
        with contextlib.suppress(OSError):
            marker.write_text(str(proc.pid))
        return "spawned"
    except Exception:
        return "failed"


def release_ingest_retry(data_dir: Path, command: RetryCommand, transcript: Path) -> None:
    """Remove the retry marker for `transcript` if this process is its waiter.

    A waiter calls this as soon as it holds the lock, before it reads, so a
    turn locked out from then on spawns a fresh waiter instead of counting
    on one that has already read. Never raises.
    """
    try:
        marker = _retry_marker(data_dir, command, transcript)
        if _read_lock_pid(marker) == os.getpid():
            marker.unlink()
    except Exception:
        pass


def claim_ingest_lock_with_wait(
    store: LonghandStore,
    timeout_s: float = 15.0,
    interval_s: float = 0.5,
    on_wait: Callable[[], None] | None = None,
) -> bool:
    """Claim the ingest lock, waiting up to `timeout_s` for a live holder.

    CLI commands use this where hooks use the non-blocking claim_ingest_lock:
    a human running `analyze` wants the command to run when the current
    ingest finishes, not to be told "try again later". `on_wait` fires once,
    the first time waiting actually begins, so callers can print a note.

    Returns True when the lock is ours; False when the timeout expired.
    Same-PID claims are idempotent (inherited from claim_ingest_lock), so
    nested wait-claims by one process return immediately instead of
    deadlocking against themselves.
    """
    deadline = time.monotonic() + timeout_s
    waited = False
    while True:
        if claim_ingest_lock(store):
            return True
        if not waited:
            waited = True
            if on_wait is not None:
                on_wait()
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)


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
