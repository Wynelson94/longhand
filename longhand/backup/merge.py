"""Reconcile one staged export against the repository: a session never shrinks.

Two machines can hold different copies of the same session (one restored,
one live-tailed, one that ingested fewer lines). The repository keeps the
union: its own lines first, then the lines only this machine had. A staged
copy that adds nothing leaves the repository untouched.

Every change to a live session directory is a directory swap: the new
contents are built beside it in a ``.merge`` sidecar (``session.json``
written last, so a sidecar that has one is complete), the live directory is
renamed to ``.old``, the sidecar renamed into place, and ``.old`` removed. A
run killed at any point leaves a state ``recover_interrupted`` can finish or
undo, and a live directory is never without its transcript.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from longhand.backup.export import (
    PART_BYTES_DEFAULT,
    SESSION_META_FILENAME,
    SESSIONS_DIR,
    SessionExport,
    read_session_json,
    read_session_lines,
    transcript_base_name,
    write_session_files,
    write_session_meta,
)
from longhand.timeutil import utcnow

OUTCOME_NEW = "new"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_SUBSET = "subset"
OUTCOME_GROWN = "grown"

MERGE_SUFFIX = ".merge"
OLD_SUFFIX = ".old"


@dataclass
class ReconcileResult:
    outcome: str
    meta: dict[str, Any]


def _digest(line: str) -> bytes:
    return hashlib.sha1(line.encode("utf-8")).digest()


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _is_sidecar(path: Path) -> bool:
    return path.name.endswith((MERGE_SUFFIX, OLD_SUFFIX))


def index_repo_sessions(repo_root: Path) -> dict[str, tuple[str, str | None]]:
    """``session_id`` → ``(rel_dir, sha256)`` for every readable ``session.json``."""
    index: dict[str, tuple[str, str | None]] = {}
    sessions_dir = repo_root / SESSIONS_DIR
    if not sessions_dir.is_dir():
        return index
    for meta_path in sorted(sessions_dir.rglob(SESSION_META_FILENAME)):
        session_dir = meta_path.parent
        if _is_sidecar(session_dir):
            continue
        try:
            meta = read_session_json(session_dir)
        except (OSError, ValueError):
            continue
        session_id = meta.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        sha = meta.get("sha256")
        rel_dir = session_dir.relative_to(repo_root).as_posix()
        index[session_id] = (rel_dir, sha if isinstance(sha, str) else None)
    return index


def recover_interrupted(repo_root: Path) -> list[str]:
    """Finish or undo the swaps a killed run left behind; returns the dirs touched.

    A ``.merge`` sidecar with a ``session.json`` is complete: when its live
    directory is missing, the only step left is the rename. Anything else
    in a sidecar is partial and goes. A ``.old`` directory is the previous
    copy: superseded when the live directory exists, restored when not.
    """
    touched: list[str] = []
    sessions_dir = repo_root / SESSIONS_DIR
    if not sessions_dir.is_dir():
        return touched
    for merge_dir in sorted(p for p in sessions_dir.rglob(f"*{MERGE_SUFFIX}") if p.is_dir()):
        live = merge_dir.with_name(merge_dir.name[: -len(MERGE_SUFFIX)])
        if (merge_dir / SESSION_META_FILENAME).exists() and not live.exists():
            os.rename(merge_dir, live)
        else:
            _discard(merge_dir)
        touched.append(live.relative_to(repo_root).as_posix())
    for old_dir in sorted(p for p in sessions_dir.rglob(f"*{OLD_SUFFIX}") if p.is_dir()):
        live = old_dir.with_name(old_dir.name[: -len(OLD_SUFFIX)])
        if live.exists():
            _discard(old_dir)
        else:
            os.rename(old_dir, live)
        touched.append(live.relative_to(repo_root).as_posix())
    return touched


def _swap_in(merge_dir: Path, repo_dir: Path) -> None:
    """Make the complete sidecar the live directory with two renames."""
    old_dir = repo_dir.with_name(repo_dir.name + OLD_SUFFIX)
    _discard(old_dir)
    if repo_dir.exists():
        os.rename(repo_dir, old_dir)
    os.rename(merge_dir, repo_dir)
    _discard(old_dir)


def reconcile_session(
    staged: SessionExport,
    staged_root: Path,
    repo_root: Path,
    *,
    part_bytes: int = PART_BYTES_DEFAULT,
    repo_rel_dir: str | None = None,
) -> ReconcileResult:
    """Fold one staged export into the repo; the staged directory is consumed either way.

    ``repo_rel_dir`` names where the repository already keeps this session
    when that differs from the staged layout (a session whose start time
    moved); the repository's location wins so a session never has two copies.

    Outcomes: ``new`` (no repo copy: the staged directory is swapped into
    place), ``unchanged`` (same sha256), ``subset`` (every staged line is
    already in the repo), ``grown`` (the union was written and
    ``session.json`` refreshed). The returned ``meta`` is the repo's
    ``session.json`` after the call.
    """
    rel_dir = repo_rel_dir or staged.rel_dir
    staged_dir = staged_root / Path(staged.rel_dir)
    repo_dir = repo_root / Path(rel_dir)
    merge_dir = repo_dir.with_name(repo_dir.name + MERGE_SUFFIX)
    _discard(merge_dir)

    if not (repo_dir / SESSION_META_FILENAME).exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_dir), str(merge_dir))
        meta = dict(staged.meta)
        if rel_dir != staged.rel_dir:
            meta["rel_dir"] = rel_dir
            write_session_meta(merge_dir, meta)
        _swap_in(merge_dir, repo_dir)  # a partial directory without session.json goes with it
        return ReconcileResult(OUTCOME_NEW, meta)

    repo_meta = read_session_json(repo_dir)
    if repo_meta.get("sha256") == staged.meta.get("sha256"):
        _discard(staged_dir)
        return ReconcileResult(OUTCOME_UNCHANGED, repo_meta)

    repo_keys = {_digest(line) for line in read_session_lines(repo_dir, repo_meta)}
    local_only: set[bytes] = set()
    for line in read_session_lines(staged_dir, staged.meta):
        key = _digest(line)
        if key not in repo_keys:
            local_only.add(key)
    if not local_only:
        _discard(staged_dir)
        return ReconcileResult(OUTCOME_SUBSET, repo_meta)

    def _union() -> Iterator[str]:
        yield from read_session_lines(repo_dir, repo_meta)
        emitted: set[bytes] = set()
        for line in read_session_lines(staged_dir, staged.meta):
            key = _digest(line)
            if key in local_only and key not in emitted:
                emitted.add(key)
                yield line

    base_name = transcript_base_name(staged.source)
    files = write_session_files(_union(), merge_dir, base_name, part_bytes=part_bytes)
    meta = dict(staged.meta)
    meta.update(
        rel_dir=rel_dir,
        parts=list(files.parts),
        lines=files.lines,
        bytes=files.bytes,
        sha256=files.sha256,
        exported_at=utcnow().isoformat(),
    )
    write_session_meta(merge_dir, meta)  # last: a sidecar with session.json is complete
    _swap_in(merge_dir, repo_dir)
    _discard(staged_dir)
    return ReconcileResult(OUTCOME_GROWN, meta)
