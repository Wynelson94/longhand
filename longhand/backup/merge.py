"""Never-shrink reconcile of a staged session export against the backup repo.

Two machines can hold the same session: the one that recorded it and one
that restored it (and re-parses fewer lines than the original stored). The
repo copy of a session therefore only ever grows. A staged export whose lines
are all already in the repo leaves the repo untouched; one with lines the
repo lacks is merged as the union, repo order first, new lines after in the
staged order.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from longhand.backup.export import (
    PART_BYTES_DEFAULT,
    SESSION_META_FILENAME,
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


@dataclass
class ReconcileResult:
    outcome: str
    meta: dict[str, Any]


def _digest(line: str) -> bytes:
    return hashlib.sha1(line.encode("utf-8")).digest()


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def reconcile_session(
    staged: SessionExport,
    staged_root: Path,
    repo_root: Path,
    *,
    part_bytes: int = PART_BYTES_DEFAULT,
) -> ReconcileResult:
    """Fold one staged export into the repo; the staged directory is consumed either way.

    Outcomes: ``new`` (no repo copy: the staged directory is moved into place),
    ``unchanged`` (same sha256), ``subset`` (every staged line is already in the
    repo), ``grown`` (the union was written and ``session.json`` refreshed).
    The returned ``meta`` is the repo's ``session.json`` after the call.
    """
    staged_dir = staged_root / Path(staged.rel_dir)
    repo_dir = repo_root / Path(staged.rel_dir)

    if not (repo_dir / SESSION_META_FILENAME).exists():
        if repo_dir.exists():
            # Parts without a session.json: an interrupted run. Start over.
            _discard(repo_dir)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_dir), str(repo_dir))
        return ReconcileResult(OUTCOME_NEW, staged.meta)

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

    # Written beside the staged copy, never in the repo dir: write_session_files
    # clears stale parts first, and the repo parts are still being read.
    base_name = transcript_base_name(staged.source)
    merge_dir = staged_dir.with_name(staged_dir.name + ".merge")
    _discard(merge_dir)
    files = write_session_files(_union(), merge_dir, base_name, part_bytes=part_bytes)
    for stale in repo_dir.glob(f"{base_name}*.jsonl"):
        stale.unlink()
    for part in files.parts:
        shutil.move(str(merge_dir / part), str(repo_dir / part))
    meta = dict(staged.meta)
    meta.update(
        parts=list(files.parts),
        lines=files.lines,
        bytes=files.bytes,
        sha256=files.sha256,
        exported_at=utcnow().isoformat(),
    )
    write_session_meta(repo_dir, meta)
    _discard(merge_dir)
    _discard(staged_dir)
    return ReconcileResult(OUTCOME_GROWN, meta)
