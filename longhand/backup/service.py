"""The backup run: config, sizing, export → reconcile → commit → push, auto mode, status.

Nothing here runs unless ``backup.enabled`` is set in ``config.json``, and only
the interactive init command writes that, after the consent screen. The run
never shrinks a session in the repository (see ``merge.py``), commits in
batches so a long first upload survives an interruption, and records what it
did in ``backup-state.json`` next to the store.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from longhand.backup.export import (
    PART_BYTES_DEFAULT,
    SESSION_META_FILENAME,
    SESSIONS_DIR,
    export_session,
    iter_session_lines,
    mask_line,
    read_manifest,
    session_source,
    write_manifest,
    write_readme_if_absent,
)
from longhand.backup.gitrepo import GitConflictError, GitError, GitRepo, redact_remote
from longhand.backup.merge import (
    OUTCOME_GROWN,
    OUTCOME_NEW,
    OUTCOME_SUBSET,
    OUTCOME_UNCHANGED,
    reconcile_session,
)
from longhand.backup.state import BackupState, changed_sessions, session_marks
from longhand.timeutil import utcnow

if TYPE_CHECKING:
    from longhand.storage.sqlite_store import SQLiteStore

CONFIG_NAMESPACE = "backup"
CONFIG_FILENAME = "config.json"
AUTO_MIN_INTERVAL_SECONDS = 3600
BATCH_BYTES_DEFAULT = 500_000_000
STAGING_DIRNAME = "backup-staging"
LOG_FILENAME = "backup.log"
LEFTOVER_MESSAGE = "longhand backup: leftover from an interrupted run"

Progress = Callable[[str], None]


class BackupDisabledError(RuntimeError):
    """Backups are not enabled on this machine, or the repository is missing."""


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
    sessions_dir = repo_root / SESSIONS_DIR
    if not sessions_dir.is_dir():
        return 0
    return sum(1 for _ in sessions_dir.rglob(SESSION_META_FILENAME))


def _manifest_stale(repo_root: Path) -> bool:
    manifest = read_manifest(repo_root)
    return manifest is None or manifest.get("sessions") != _count_sessions(repo_root)


def _copy_config(data_dir: Path, repo_root: Path) -> None:
    source = data_dir / CONFIG_FILENAME
    if not source.is_file():
        return
    payload = source.read_bytes()
    target = repo_root / CONFIG_FILENAME
    if not target.exists() or target.read_bytes() != payload:
        target.write_bytes(payload)


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

    Raises ``BackupDisabledError`` unless backups are enabled and the repository
    exists. A push failure is reported, never raised: the commits stay local
    and the next run pushes them.
    """
    say: Progress = progress or (lambda message: None)
    config = BackupConfig.load(data_dir)
    if not config.enabled:
        raise BackupDisabledError("backups are off on this machine; run longhand backup init first")
    repo = GitRepo(config.repo_path(data_dir))
    if not repo.exists():
        raise BackupDisabledError(
            f"no backup repository at {repo.path}; run longhand backup init first"
        )

    state = BackupState.load(data_dir)
    marks = session_marks(sqlite)
    changed = changed_sessions(marks, state)
    say(f"{len(changed)} session(s) changed since the last run")

    if dry_run:

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

    report = RunReport(planned=len(changed))
    committer = _Committer(repo, push=push, say=say)
    errors: list[str] = []

    if repo.has_changes():
        say("committing files left behind by an interrupted run")
        committer.commit(LEFTOVER_MESSAGE)

    try:
        if repo.fetch_and_ff():
            say("brought the repository up to date with the remote")
    except GitConflictError as exc:
        errors.append(str(exc))
        return _finish(state, report, committer, errors, data_dir)
    except GitError as exc:
        errors.append(f"fetch failed: {exc}")
        say("fetch failed; continuing without the remote")

    staging = data_dir / STAGING_DIRNAME
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    committed_added = committed_grown = 0

    def _message() -> str:
        added = report.added - committed_added
        grown = report.grown - committed_grown
        return f"longhand backup: {added} added, {grown} updated"

    try:
        _copy_config(data_dir, repo.path)
        write_readme_if_absent(repo.path)
        batch = 0
        for index, session_id in enumerate(changed, 1):
            row = sqlite.get_session(session_id)
            if row is None:
                continue
            exported = export_session(sqlite, row, staging, part_bytes=part_bytes, mask=mask)
            result = reconcile_session(exported, staging, repo.path, part_bytes=part_bytes)
            _tally(report, result.outcome)
            report.bytes_exported += exported.files.bytes
            if exported.mask_counts:
                report.sessions_masked += 1
                _add_counts(report.mask_counts, exported.mask_counts)
            state.sessions[session_id] = replace(
                marks[session_id], sha256=result.meta.get("sha256")
            )
            say(f"{index}/{len(changed)} {session_id}: {result.outcome}")
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

    return _finish(state, report, committer, errors, data_dir)


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
    last_run = _parse_iso(state.last_run_at)
    last_push = _parse_iso(state.last_push_at)
    if state.last_error and last_run is not None and (last_push is None or last_push < last_run):
        return f"{mode} · last push FAILED {_age_text(state.last_run_at)}"
    if last_push is None:
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
            f"{report.commits} commit(s), pushed={report.pushed}"
        )
        if report.error:
            summary += f"; error: {report.error}"
        log_line(data_dir, summary)
        return report
    except Exception as exc:
        log_line(data_dir, f"auto backup failed: {exc}")
        return None
