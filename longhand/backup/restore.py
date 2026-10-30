"""Restore the archive from a backup repository.

The contract is the lines, not the rows: every exported transcript is parsed
again by the current parser (``JSONLParser`` reads Claude Code transcripts and
Codex rollouts alike), so a restored store is the current parser's view of the
record, exactly as ``analyze --all`` re-extracts with the current extractors.
Vectors and derived analysis are rebuilt by the ingest pipeline; the backup
never held them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from longhand.backup.export import (
    BACKUP_FORMAT,
    MANIFEST_FILENAME,
    SESSION_META_FILENAME,
    SESSIONS_DIR,
    read_manifest,
    read_session_json,
    session_dir_name,
)
from longhand.backup.service import CONFIG_FILENAME, CONFIG_NAMESPACE
from longhand.backup.state import BackupState, SessionMark, session_marks
from longhand.parser import JSONLParser

if TYPE_CHECKING:
    from longhand.storage import LonghandStore

WORK_DIRNAME = "backup-restore"

Progress = Callable[[str], None]


@dataclass
class RestoreReport:
    imported: int = 0
    skipped: int = 0
    events: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    config_copied: bool = False


def iter_backup_sessions(repo_root: Path) -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every ``sessions/**/session.json`` in path order; an unreadable one yields ``{}``."""
    sessions_dir = repo_root / SESSIONS_DIR
    if not sessions_dir.is_dir():
        return
    for meta_path in sorted(sessions_dir.rglob(SESSION_META_FILENAME)):
        try:
            meta = read_session_json(meta_path.parent)
        except (OSError, ValueError):
            meta = {}
        yield meta_path.parent, meta


def _part_path(session_dir: Path, part: str) -> Path:
    path = session_dir / part
    if path.resolve().parent != session_dir.resolve():
        raise ValueError(f"part {part!r} is not inside the session directory")
    return path


def materialize_transcript(session_dir: Path, meta: dict[str, Any], work_dir: Path) -> Path:
    """The file to parse: the single part itself, or the parts joined under ``work_dir``."""
    parts = [str(p) for p in meta.get("parts") or []]
    if not parts:
        raise ValueError("session.json lists no parts")
    if len(parts) == 1:
        return _part_path(session_dir, parts[0])
    work_dir.mkdir(parents=True, exist_ok=True)
    name = session_dir_name(str(meta.get("session_id") or session_dir.name))
    target = work_dir / f"{name}.jsonl"
    with target.open("wb") as out:
        for part in parts:
            with _part_path(session_dir, part).open("rb") as src:
                shutil.copyfileobj(src, out)
    return target


def check_transcript(session_dir: Path, meta: dict[str, Any]) -> tuple[bool, int]:
    """Stream the parts once: does their sha256 match ``session.json``, and how many
    lines are longer than the parser will take (those are skipped on ingest)."""
    import longhand.parser as parser_mod

    digest = hashlib.sha256()
    oversized = 0
    limit = parser_mod.MAX_LINE_LENGTH
    for part in [str(p) for p in meta.get("parts") or []]:
        with _part_path(session_dir, part).open("rb") as fh:
            for raw in fh:
                digest.update(raw)
                if len(raw) > limit and len(raw.decode("utf-8", "replace").rstrip("\n")) > limit:
                    oversized += 1
    expected = meta.get("sha256")
    return (not isinstance(expected, str) or digest.hexdigest() == expected), oversized


def copy_config_if_absent(repo_root: Path, data_dir: Path) -> bool:
    """Bring ``config.json`` over when this data dir has none; the backup namespace stays behind."""
    source = repo_root / CONFIG_FILENAME
    target = data_dir / CONFIG_FILENAME
    if target.exists() or not source.is_file():
        return False
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    data.pop(CONFIG_NAMESPACE, None)  # the old machine's clone path and switch do not carry over
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True


def _check_backup(repo_root: Path) -> None:
    manifest = read_manifest(repo_root)
    if manifest is None and not (repo_root / SESSIONS_DIR).is_dir():
        raise ValueError(
            f"{repo_root} is not a Longhand backup "
            f"(no {MANIFEST_FILENAME} and no {SESSIONS_DIR}/ directory)"
        )
    if manifest is not None:
        fmt = manifest.get("format", BACKUP_FORMAT)
        if isinstance(fmt, int) and fmt > BACKUP_FORMAT:
            raise ValueError(
                f"this backup is format {fmt}; this Longhand reads up to format "
                f"{BACKUP_FORMAT}. Upgrade Longhand, then restore."
            )


def restore_backup(
    store: LonghandStore,
    repo_root: Path,
    *,
    skip_analysis: bool = False,
    progress: Progress | None = None,
) -> RestoreReport:
    """Import every session the repository holds that this store does not already have.

    A session is skipped when the local state records the same sha256 (an earlier
    restore or backup run on this machine). One broken session is recorded in
    ``errors`` and never aborts the rest. Raises ``ValueError`` when the
    directory is not a backup or is a newer format, ``RuntimeError`` when
    another ingest holds the lock.
    """
    from longhand.recall.project_fallback import (
        claim_ingest_lock_with_wait,
        release_ingest_lock,
    )

    say: Progress = progress or (lambda message: None)
    repo_root = Path(repo_root)
    _check_backup(repo_root)
    if not claim_ingest_lock_with_wait(
        store, on_wait=lambda: say("waiting for a running ingest to finish")
    ):
        raise RuntimeError("another Longhand ingest is running; try again shortly")

    report = RestoreReport()
    work_dir = store.data_dir / WORK_DIRNAME
    shutil.rmtree(work_dir, ignore_errors=True)
    try:
        state = BackupState.load(store.data_dir)
        imported: dict[str, str] = {}
        entries = list(iter_backup_sessions(repo_root))
        for index, (session_dir, meta) in enumerate(entries, 1):
            label = str(session_dir.relative_to(repo_root))
            session_id = meta.get("session_id")
            sha = meta.get("sha256")
            if not isinstance(session_id, str) or not session_id or not meta.get("parts"):
                report.errors.append(
                    {"session": label, "error": "unreadable session.json (no session_id or parts)"}
                )
                continue
            mark = state.sessions.get(session_id)
            if mark is not None and mark.sha256 and mark.sha256 == sha:
                report.skipped += 1
                continue
            try:
                intact, oversized = check_transcript(session_dir, meta)
                if not intact:
                    raise ValueError("transcript does not match session.json sha256 (damaged copy)")
                path = materialize_transcript(session_dir, meta, work_dir)
                try:
                    parser = JSONLParser(path)
                    events = list(parser.parse_events())
                    if not events:
                        raise ValueError("no events the current parser keeps")
                    session = parser.build_session(events)
                    session.transcript_path = str(session_dir / str(meta["parts"][0]))
                    store.ingest_session(session, events, run_analysis=not skip_analysis)
                finally:
                    if path.parent == work_dir:
                        path.unlink(missing_ok=True)
            except Exception as exc:
                report.errors.append(
                    {"session": f"{session_id} ({label})", "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if oversized:
                report.errors.append(
                    {
                        "session": f"{session_id} ({label})",
                        "error": (
                            f"{oversized} line(s) over the parser's line-length limit were not "
                            "restored; the rest of the session was"
                        ),
                    }
                )
            report.imported += 1
            report.events += len(events)
            if isinstance(sha, str) and sha:
                imported[session.session_id] = sha
            say(f"{index}/{len(entries)} {session_id}: {len(events)} events")
        if imported:
            marks = session_marks(store.sqlite)
            for session_id, sha in imported.items():
                mark = marks.get(session_id)
                if mark is None:
                    # Every event of this session now belongs to a session imported
                    # later that shares its event ids (the store keeps one row per
                    # id). It is marked anyway: otherwise every restore would import
                    # it again and the shared rows would flip back and forth.
                    row = store.sqlite.get_session(session_id)
                    ingested = row.get("ingested_at") if row else None
                    mark = SessionMark(
                        event_count=0,
                        last_event_timestamp=None,
                        ingested_at=str(ingested) if ingested else None,
                    )
                state.sessions[session_id] = replace(mark, sha256=sha)
            state.save(store.data_dir)
        report.config_copied = copy_config_if_absent(repo_root, store.data_dir)
    finally:
        release_ingest_lock(store)
        shutil.rmtree(work_dir, ignore_errors=True)
    return report
