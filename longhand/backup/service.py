"""The backup run: config, sizing, export → reconcile → commit → push, auto mode, status.

Nothing here runs unless ``backup.enabled`` is set in ``config.json``, and only
the interactive init command writes that, after the consent screen. The run
never shrinks a session in the repository (see ``merge.py``), commits in
batches so a long first upload survives an interruption, and records what it
did in ``backup-state.json`` next to the store.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from longhand.backup.export import (
    MANIFEST_FILENAME,
    PART_BYTES_DEFAULT,
    SESSIONS_DIR,
    export_session,
    iter_session_lines,
    mask_line,
    read_manifest,
    session_rel_dir,
    session_source,
    write_gitignore_if_absent,
    write_manifest,
    write_readme_if_absent,
)
from longhand.backup.gitrepo import GitError, GitRepo, redact_remote
from longhand.backup.merge import (
    OUTCOME_GROWN,
    OUTCOME_NEW,
    OUTCOME_SUBSET,
    OUTCOME_UNCHANGED,
    index_repo_sessions,
    reconcile_session,
    recover_interrupted,
)
from longhand.backup.state import BackupState, changed_sessions, session_marks
from longhand.timeutil import utcnow

if TYPE_CHECKING:
    from longhand.storage.sqlite_store import SQLiteStore

CONFIG_NAMESPACE = "backup"
CONFIG_FILENAME = "config.json"
AUTO_MIN_INTERVAL_SECONDS = 3600
BATCH_BYTES_DEFAULT = 500_000_000
STAGING_DIRNAME = ".staging"  # inside the clone (ignored there), so every move is a rename
LOCK_FILENAME = "backup.lock"
LOG_FILENAME = "backup.log"
LEFTOVER_MESSAGE = "longhand backup: leftover from an interrupted run"

Progress = Callable[[str], None]


class BackupDisabledError(RuntimeError):
    """Backups are not enabled on this machine, or the repository is missing."""


class BackupBusyError(RuntimeError):
    """Another backup run holds the lock."""


@contextmanager
def _backup_lock(data_dir: Path) -> Iterator[None]:
    """Exclusive lock for one run; a lock left by a dead process is taken over."""
    from longhand.recall.project_fallback import _lock_holder_alive

    path = data_dir / LOCK_FILENAME
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            holder: int | None = None
            try:
                holder = int(path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                holder = None
            if holder and _lock_holder_alive(holder):
                raise BackupBusyError(
                    f"another backup run is in progress (pid {holder}); try again later"
                ) from None
            if attempt == 0:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            raise BackupBusyError("could not take the backup lock") from None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n")
        break
    try:
        yield
    finally:
        try:
            path.unlink()
        except OSError:
            pass


# ─── Config ───────────────────────────────────────────────────────────────────


def _read_config(data_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads((data_dir / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@dataclass
class BackupConfig:
    enabled: bool = False
    remote: str | None = None
    path: str | None = None
    auto: bool = False

    @classmethod
    def load(cls, data_dir: Path) -> BackupConfig:
        """The ``backup`` namespace of ``config.json``; defaults when missing or malformed."""
        section = _read_config(data_dir).get(CONFIG_NAMESPACE)
        if not isinstance(section, dict):
            return cls()
        enabled = section.get("enabled", False)
        auto = section.get("auto", False)
        remote = section.get("remote")
        path = section.get("path")
        if not isinstance(enabled, bool) or not isinstance(auto, bool):
            return cls()
        if remote is not None and not isinstance(remote, str):
            return cls()
        if path is not None and not isinstance(path, str):
            return cls()
        return cls(enabled=enabled, remote=remote, path=path, auto=auto)

    def save(self, data_dir: Path) -> None:
        """Merge into ``config.json``, keeping every other namespace as it is."""
        config_path = data_dir / CONFIG_FILENAME
        data: dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ValueError(
                    f"{config_path} is not valid JSON; fix it before changing backup settings"
                ) from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} is not a JSON object")
            data = loaded
        data[CONFIG_NAMESPACE] = {
            "enabled": self.enabled,
            "remote": self.remote,
            "path": self.path,
            "auto": self.auto,
        }
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def repo_path(self, data_dir: Path) -> Path:
        return Path(self.path).expanduser() if self.path else data_dir / "backup"


# ─── Sizing ───────────────────────────────────────────────────────────────────


@dataclass
class SizeEstimate:
    sessions: int = 0
    claude: int = 0
    codex: int = 0
    lines: int = 0
    bytes: int = 0
    mask_counts: dict[str, int] = field(default_factory=dict)
    sessions_with_masks: int = 0


def _add_counts(into: dict[str, int], counts: dict[str, int]) -> None:
    for name, n in counts.items():
        into[name] = into.get(name, 0) + n


def estimate_export(
    sqlite: SQLiteStore,
    session_ids: Iterable[str] | None = None,
    *,
    mask: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> SizeEstimate:
    """What an export of these sessions (default: all) would write, measured by the same pass."""
    ids = list(session_ids) if session_ids is not None else sorted(session_marks(sqlite))
    estimate = SizeEstimate()
    for index, session_id in enumerate(ids, 1):
        masked_here = False
        for raw in iter_session_lines(sqlite, session_id):
            line = raw
            if mask:
                line, counts = mask_line(raw)
                if counts:
                    masked_here = True
                    _add_counts(estimate.mask_counts, counts)
            estimate.lines += 1
            estimate.bytes += len(line.encode("utf-8")) + 1
        estimate.sessions += 1
        if session_source(session_id) == "codex":
            estimate.codex += 1
        else:
            estimate.claude += 1
        if masked_here:
            estimate.sessions_with_masks += 1
        if progress is not None:
            progress(index, len(ids))
    return estimate


# ─── The run ──────────────────────────────────────────────────────────────────


@dataclass
class RunReport:
    planned: int = 0
    added: int = 0
    grown: int = 0
    unchanged: int = 0
    subset: int = 0
    bytes_exported: int = 0
    mask_counts: dict[str, int] = field(default_factory=dict)
    sessions_masked: int = 0
    commits: int = 0
    pushed: bool = False
    error: str | None = None
    dry_run: bool = False
    failed: int = 0


class _Committer:
    """Commits and pushes for one run; stops pushing after the first push failure."""

    def __init__(self, repo: GitRepo, *, push: bool, say: Progress) -> None:
        self.repo = repo
        self.push = push
        self.say = say
        self.commits = 0
        self.pushed = False
        self.errors: list[str] = []

    def commit(self, message: str) -> bool:
        self.repo.add_all()
        if not self.repo.commit(message):
            return False
        self.commits += 1
        self.say(f"committed: {message}")
        return True

    def push_if_needed(self) -> None:
        if not self.push or self.repo.unpushed_commits() == 0:
            return
        try:
            self.repo.push()
        except GitError as exc:
            self.errors.append(f"push failed: {exc}")
            self.push = False
            self.pushed = False
            self.say("push failed; later commits stay local until the next run")
        else:
            self.pushed = True
            self.say("pushed")

    def commit_and_push(self, message: str) -> None:
        self.commit(message)
        self.push_if_needed()


def _count_sessions(repo_root: Path) -> int:
    return len(index_repo_sessions(repo_root))


def _manifest_stale(repo_root: Path) -> bool:
    manifest = read_manifest(repo_root)
    return manifest is None or manifest.get("sessions") != _count_sessions(repo_root)


def _copy_config(data_dir: Path, repo_root: Path) -> None:
    """Put ``config.json`` in the repo minus the backup namespace, which is machine-specific."""
    source = data_dir / CONFIG_FILENAME
    if not source.is_file():
        return
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    data.pop(CONFIG_NAMESPACE, None)
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    target = repo_root / CONFIG_FILENAME
    if not target.exists() or target.read_bytes() != payload:
        target.write_bytes(payload)


def is_backup_repo(repo: GitRepo) -> bool:
    """An empty repository, or one that already holds a Longhand backup."""
    if repo.head() is None:
        return True
    return (repo.path / MANIFEST_FILENAME).exists() or (repo.path / SESSIONS_DIR).is_dir()


def check_repo(repo: GitRepo, config: BackupConfig) -> None:
    """Refuse a clone that is not the one init validated: wrong origin, or not a backup."""
    if not repo.exists():
        raise BackupDisabledError(
            f"no backup repository at {repo.path}; run longhand backup init first"
        )
    origin = repo.run("remote", "get-url", "origin", check=False).stdout.strip()
    if not config.remote or origin != config.remote:
        raise BackupDisabledError(
            f"the clone at {repo.path} points at {redact_remote(origin) or 'no remote'}, not "
            f"backup.remote ({redact_remote(config.remote) if config.remote else 'unset'}); "
            "run longhand backup init again"
        )
    if not is_backup_repo(repo):
        raise BackupDisabledError(
            f"{repo.path} is not a Longhand backup repository (no manifest.json or sessions/ "
            "and a history of its own); choose another --path at longhand backup init"
        )


def prune_marks(state: BackupState, index: dict[str, tuple[str, str | None]]) -> list[str]:
    """Drop every mark the clone does not back: missing session, or a different sha256.

    The state file says what was exported; the clone says what is actually
    there. After a re-created clone, a new --path, or a reset to the remote,
    the two disagree, and the clone wins: those sessions are exported again.
    """
    dropped = []
    for session_id, mark in list(state.sessions.items()):
        entry = index.get(session_id)
        if entry is None or entry[1] != mark.sha256:
            del state.sessions[session_id]
            dropped.append(session_id)
    return sorted(dropped)


def _tally(report: RunReport, outcome: str) -> None:
    if outcome == OUTCOME_NEW:
        report.added += 1
    elif outcome == OUTCOME_GROWN:
        report.grown += 1
    elif outcome == OUTCOME_SUBSET:
        report.subset += 1
    elif outcome == OUTCOME_UNCHANGED:
        report.unchanged += 1


def _finish(
    state: BackupState, report: RunReport, committer: _Committer, errors: list[str], data_dir: Path
) -> RunReport:
    now = utcnow().isoformat()
    report.commits = committer.commits
    report.pushed = committer.pushed
    all_errors = errors + committer.errors
    report.error = "; ".join(all_errors) if all_errors else None
    state.last_run_at = now
    if report.pushed:
        state.last_push_at = now
    state.last_error = report.error
    state.last_mask_counts = dict(report.mask_counts)
    state.last_run_summary = {
        "planned": report.planned,
        "added": report.added,
        "grown": report.grown,
        "unchanged": report.unchanged,
        "subset": report.subset,
        "commits": report.commits,
    }
    state.save(data_dir)
    return report


def run_backup(
    sqlite: SQLiteStore,
    data_dir: Path,
    *,
    dry_run: bool = False,
    push: bool = True,
    mask: bool = True,
    batch_bytes: int = BATCH_BYTES_DEFAULT,
    part_bytes: int = PART_BYTES_DEFAULT,
    progress: Progress | None = None,
) -> RunReport:
    """Export every changed session into the backup repository, commit, push.

    Raises ``BackupDisabledError`` unless backups are enabled and the clone
    is the one init validated, ``BackupBusyError`` while another run holds
    the lock. A push failure or a session that fails to export is reported,
    never raised: the commits stay local and the next run pushes them, the
    session is tried again next time. Any other failure is recorded in the
    state before it propagates, so doctor shows it.
    """
    say: Progress = progress or (lambda message: None)
    config = BackupConfig.load(data_dir)
    if not config.enabled:
        raise BackupDisabledError("backups are off on this machine; run longhand backup init first")
    repo = GitRepo(config.repo_path(data_dir))
    check_repo(repo, config)

    state = BackupState.load(data_dir)
    if dry_run:
        prune_marks(state, index_repo_sessions(repo.path))
        changed = changed_sessions(session_marks(sqlite), state)
        say(f"{len(changed)} session(s) to export")

        def _measured(index: int, total: int) -> None:
            if index == total or index % 25 == 0:
                say(f"measured {index}/{total}")

        estimate = estimate_export(sqlite, changed, mask=mask, progress=_measured)
        return RunReport(
            planned=len(changed),
            bytes_exported=estimate.bytes,
            mask_counts=estimate.mask_counts,
            sessions_masked=estimate.sessions_with_masks,
            dry_run=True,
        )

    with _backup_lock(data_dir):
        report = RunReport()
        committer = _Committer(repo, push=push, say=say)
        errors: list[str] = []
        try:
            _run_locked(
                sqlite,
                data_dir,
                repo,
                state,
                report,
                committer,
                errors,
                mask=mask,
                batch_bytes=batch_bytes,
                part_bytes=part_bytes,
                say=say,
            )
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            _finish(state, report, committer, errors, data_dir)
            raise
        return _finish(state, report, committer, errors, data_dir)


def _run_locked(
    sqlite: SQLiteStore,
    data_dir: Path,
    repo: GitRepo,
    state: BackupState,
    report: RunReport,
    committer: _Committer,
    errors: list[str],
    *,
    mask: bool,
    batch_bytes: int,
    part_bytes: int,
    say: Progress,
) -> None:
    for touched in recover_interrupted(repo.path):
        say(f"finished an interrupted swap: {touched}")
    staging = repo.path / STAGING_DIRNAME
    shutil.rmtree(staging, ignore_errors=True)

    if repo.has_changes():
        say("committing files left behind by an interrupted run")
        committer.commit(LEFTOVER_MESSAGE)

    try:
        outcome = repo.fetch_and_ff()
    except GitError as exc:
        errors.append(f"fetch failed: {exc}")
        say("fetch failed; continuing without the remote")
    else:
        if outcome == "fast-forward":
            say("brought the repository up to date with the remote")
        elif outcome == "reset":
            say("the remote and this clone had both moved: took the remote, re-exporting")

    index = index_repo_sessions(repo.path)
    pruned = prune_marks(state, index)
    if pruned:
        say(f"{len(pruned)} session(s) no longer in the clone will be exported again")
    marks = session_marks(sqlite)
    changed = changed_sessions(marks, state)
    report.planned = len(changed)
    say(f"{len(changed)} session(s) to export")

    staging.mkdir(parents=True, exist_ok=True)
    committed_added = committed_grown = 0

    def _message() -> str:
        added = report.added - committed_added
        grown = report.grown - committed_grown
        return f"longhand backup: {added} added, {grown} updated"

    try:
        write_gitignore_if_absent(repo.path)
        _copy_config(data_dir, repo.path)
        write_readme_if_absent(repo.path)
        batch = 0
        for position, session_id in enumerate(changed, 1):
            row = sqlite.get_session(session_id)
            if row is None:
                continue
            kept = index.get(session_id)
            try:
                exported = export_session(sqlite, row, staging, part_bytes=part_bytes, mask=mask)
                result = reconcile_session(
                    exported,
                    staging,
                    repo.path,
                    part_bytes=part_bytes,
                    repo_rel_dir=kept[0] if kept else None,
                )
            except Exception as exc:
                report.failed += 1
                state.sessions.pop(session_id, None)
                errors.append(f"{session_id}: {type(exc).__name__}: {exc}")
                say(f"{position}/{len(changed)} {session_id}: FAILED ({type(exc).__name__})")
                shutil.rmtree(staging / Path(session_rel_dir(row)), ignore_errors=True)
                continue
            _tally(report, result.outcome)
            report.bytes_exported += exported.files.bytes
            if exported.mask_counts:
                report.sessions_masked += 1
                _add_counts(report.mask_counts, exported.mask_counts)
            state.sessions[session_id] = replace(
                marks[session_id], sha256=result.meta.get("sha256")
            )
            say(f"{position}/{len(changed)} {session_id}: {result.outcome}")
            batch += exported.files.bytes
            if batch >= batch_bytes:
                write_manifest(repo.path, _count_sessions(repo.path))
                if committer.commit(_message()):
                    committed_added, committed_grown = report.added, report.grown
                committer.push_if_needed()
                state.save(data_dir)
                batch = 0
        if batch > 0 or _manifest_stale(repo.path):
            write_manifest(repo.path, _count_sessions(repo.path))
        committer.commit_and_push(_message())
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ─── Status ───────────────────────────────────────────────────────────────────


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_text(iso_timestamp: str | None) -> str:
    """'3 min ago' style age for a stored ISO timestamp; '' when unparseable."""
    then = _parse_iso(iso_timestamp)
    if then is None:
        return ""
    seconds = max(0, int((utcnow() - then).total_seconds()))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def backup_status(data_dir: Path, sqlite: SQLiteStore) -> dict[str, Any]:
    config = BackupConfig.load(data_dir)
    state = BackupState.load(data_dir)
    repo = GitRepo(config.repo_path(data_dir))
    exists = repo.exists()
    if exists:
        prune_marks(state, index_repo_sessions(repo.path))  # read-only: never saved here
    pending = len(changed_sessions(session_marks(sqlite), state)) if config.enabled else 0
    unpushed = 0
    if exists:
        try:
            unpushed = repo.unpushed_commits()
        except GitError:
            unpushed = 0
    return {
        "enabled": config.enabled,
        "auto": config.auto,
        "remote": redact_remote(config.remote) if config.remote else None,
        "path": str(repo.path),
        "repo_exists": exists,
        "last_run_at": state.last_run_at,
        "last_push_at": state.last_push_at,
        "last_error": state.last_error,
        "pending": pending,
        "sessions_backed_up": len(state.sessions),
        "last_mask_counts": dict(state.last_mask_counts),
        "repo_bytes": repo.git_dir_bytes() if exists else 0,
        "unpushed_commits": unpushed,
    }


def backup_status_line(data_dir: Path) -> str:
    """One cell for doctor: ``off`` | ``on · never pushed`` | ``auto · last push 3 h ago`` …"""
    config = BackupConfig.load(data_dir)
    if not config.enabled:
        return "off"
    state = BackupState.load(data_dir)
    mode = "auto" if config.auto else "on"
    if state.last_error:
        return f"{mode} · last run FAILED {_age_text(state.last_run_at)}"
    if state.last_push_at is None:
        return f"{mode} · never pushed"
    return f"{mode} · last push {_age_text(state.last_push_at)}"


# ─── Log and auto mode ────────────────────────────────────────────────────────


def log_line(data_dir: Path, message: str) -> None:
    """Append one timestamped line to ``logs/backup.log``. Never raises."""
    try:
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = utcnow().isoformat(timespec="seconds")
        with (logs / LOG_FILENAME).open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def maybe_auto_backup(
    sqlite: SQLiteStore, data_dir: Path, *, now: datetime | None = None
) -> RunReport | None:
    """The reconciler's hook: back up when enabled, auto, due, and something changed.

    Masking is always on. Every failure is logged and swallowed; the caller is a
    scheduled job that must never die because the network did.
    """
    try:
        config = BackupConfig.load(data_dir)
        if not (config.enabled and config.auto):
            return None
        state = BackupState.load(data_dir)
        current = now or utcnow()
        last_push = _parse_iso(state.last_push_at)
        if last_push is not None:
            if (current - last_push).total_seconds() < AUTO_MIN_INTERVAL_SECONDS:
                return None
        if not changed_sessions(session_marks(sqlite), state):
            return None
        report = run_backup(sqlite, data_dir, mask=True, push=True)
        summary = (
            f"auto backup: {report.added} added, {report.grown} updated, "
            f"{report.failed} failed, {report.commits} commit(s), pushed={report.pushed}"
        )
        if report.error:
            summary += f"; error: {report.error}"
        log_line(data_dir, summary)
        return report
    except Exception as exc:
        log_line(data_dir, f"auto backup failed: {exc}")
        return None
