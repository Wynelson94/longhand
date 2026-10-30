"""Per-session export of the verbatim record for the ``backup`` command.

Longhand stores every transcript line verbatim in ``events.raw_json``
(``json.dumps(event.raw)``). Writing those lines back out per session
recreates the transcript the parser ingested, and the current parser is the
restore path. Sub-events split from one line (thinking / text / tool_use
blocks) share the line, so the export keeps the first copy of each distinct
line and nothing else.

Secret-shaped strings are masked in the copy only (``mask_line``); the local
store stays verbatim. Counts per pattern are reported, never values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from longhand.redaction import redact_text, scan_text
from longhand.timeutil import utcnow
from longhand.version import __version__

if TYPE_CHECKING:
    from longhand.storage.sqlite_store import SQLiteStore

BACKUP_FORMAT = 1
# Under GitHub's 50 MiB per-file warning and far under its 100 MiB block.
PART_BYTES_DEFAULT = 40_000_000
SESSIONS_DIR = "sessions"
MANIFEST_FILENAME = "manifest.json"
SESSION_META_FILENAME = "session.json"
README_FILENAME = "README.md"
GITIGNORE_FILENAME = ".gitignore"
CODEX_PREFIX = "codex:"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")

# The order `get_events` reads a session in: live-tail ingests assign
# sequences from the tail, so sequence alone does not reproduce file order.
_LINES_SQL = (
    "SELECT raw_json, timestamp FROM events WHERE session_id = ? "
    "ORDER BY timestamp, sequence, rowid"
)

_README_TEXT = """# Longhand backup

This repository is a backup of a Longhand archive: the verbatim transcript
lines of every Claude Code and Codex session, one directory per session
under `sessions/`, plus the Longhand `config.json` when one existed.

Secret-shaped strings were masked in this copy unless the backup was run
with `--no-mask`; the machine that made the backup keeps the verbatim
record. Nothing here is encrypted: whoever can read this repository can read
the transcripts, including prompts, thinking, tool output and file contents.

To rebuild a Longhand store from it on another machine, install Longhand
and run:

```bash
longhand restore --from <this repository's URL>
```

`manifest.json` records the backup format and the session count. Each
`session.json` carries the session row and the sha256 of its transcript.
Do not edit the transcript files by hand: Longhand treats the lines as the
record and never shrinks a session it already holds.
"""


# ─── Layout ───────────────────────────────────────────────────────────────────


def session_source(session_id: str) -> str:
    """``codex`` for Codex threads (``codex:<thread-id>``), else ``claude``."""
    return "codex" if session_id.startswith(CODEX_PREFIX) else "claude"


def transcript_base_name(source: str) -> str:
    """``rollout`` for Codex (the file name Codex itself uses), ``transcript`` for Claude Code."""
    return "rollout" if source == "codex" else "transcript"


def session_dir_name(session_id: str) -> str:
    """A directory name that is safe on every filesystem git runs on.

    The ``codex:`` prefix is dropped (the source is a parent directory) and
    anything outside ``[A-Za-z0-9._-]`` becomes ``_``.
    """
    bare = session_id[len(CODEX_PREFIX) :] if session_id.startswith(CODEX_PREFIX) else session_id
    name = _UNSAFE_CHARS.sub("_", bare)
    if not name.strip("."):
        # "" / "." / ".." would escape the sessions tree; name it by digest.
        name = "session-" + hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:16]
    return name


def _month_bucket(started_at: Any) -> str:
    """``YYYY/MM`` of the session start in UTC, or ``undated``."""
    if not isinstance(started_at, str) or not started_at:
        return "undated"
    text = started_at[:-1] + "+00:00" if started_at.endswith("Z") else started_at
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return "undated"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year <= 1970:
        # build_session stamps a session with no real timestamps at the epoch.
        return "undated"
    return f"{dt.year:04d}/{dt.month:02d}"


def session_rel_dir(row: dict[str, Any]) -> str:
    """``sessions/<source>/<YYYY>/<MM>/<dir>`` for a ``sessions`` row (posix separators)."""
    session_id = str(row.get("session_id") or "")
    return "/".join(
        [
            SESSIONS_DIR,
            session_source(session_id),
            _month_bucket(row.get("started_at")),
            session_dir_name(session_id),
        ]
    )


# ─── Lines ────────────────────────────────────────────────────────────────────


class _SessionLines:
    """Distinct ``raw_json`` lines of one session in store order, streamed.

    Dedupe is by sha1 of the line: sub-events from one transcript line and the
    parser's ``#``-suffixed collision rows all share it. ``last_timestamp`` is
    the newest event timestamp seen, duplicates included, once iteration ends.
    """

    def __init__(self, sqlite: SQLiteStore, session_id: str):
        self._sqlite = sqlite
        self._session_id = session_id
        self.last_timestamp: str | None = None

    def __iter__(self) -> Iterator[str]:
        seen: set[bytes] = set()
        with self._sqlite.connect() as conn:
            cursor = conn.execute(_LINES_SQL, (self._session_id,))
            for row in cursor:
                raw, timestamp = row[0], row[1]
                if isinstance(timestamp, str):
                    self.last_timestamp = timestamp
                if not isinstance(raw, str):
                    continue
                key = hashlib.sha1(raw.encode("utf-8")).digest()
                if key in seen:
                    continue
                seen.add(key)
                yield raw


def iter_session_lines(sqlite: SQLiteStore, session_id: str) -> Iterator[str]:
    """Distinct ``raw_json`` lines of one session in ``get_events`` order."""
    return iter(_SessionLines(sqlite, session_id))


# ─── Masking ──────────────────────────────────────────────────────────────────


PRIVATE_KEY_PATTERN = "ssh_private_key"
_PEM_RE = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z]+ )*)PRIVATE KEY-----"
    r"(?:[\s\S]*?-----END (?:[A-Z]+ )*PRIVATE KEY-----|[\s\S]*)"
)


def _mask_private_keys(text: str) -> tuple[str, int]:
    """Replace every private-key block, header to footer, with a mask of its length."""
    if "PRIVATE KEY-----" not in text:
        return text, 0

    def _sub(match: re.Match[str]) -> str:
        kind = match.group("kind").strip()
        label = f"{kind} PRIVATE KEY" if kind else "PRIVATE KEY"
        return f"-----{label} MASKED (len={len(match.group(0))})-----"

    return _PEM_RE.subn(_sub, text)


def _mask_obj(obj: Any, counts: dict[str, int]) -> Any:
    """Mask every secret-shaped string in a parsed line (keys included), tallying per pattern."""
    if isinstance(obj, str):
        text, keys = _mask_private_keys(obj)
        if keys:
            counts[PRIVATE_KEY_PATTERN] = counts.get(PRIVATE_KEY_PATTERN, 0) + keys
        hits = scan_text(text)
        if not hits:
            return text
        for name, n in hits.items():
            counts[name] = counts.get(name, 0) + n
        masked, _ = redact_text(text)
        return masked
    if isinstance(obj, dict):
        return {
            _mask_obj(k, counts) if isinstance(k, str) else k: _mask_obj(v, counts)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_obj(v, counts) for v in obj]
    return obj


def mask_line(raw_json: str) -> tuple[str, dict[str, int]]:
    """The line with secret-shaped strings masked, and the per-pattern counts.

    Every line is parsed and masked string by string, so the patterns see the
    real text (a token after a newline is ``\\n`` + token in the serialised
    line, where a word boundary cannot match). A line without a hit comes
    back unchanged, byte for byte; a masked line is re-serialised with the
    same ``json.dumps`` call ``insert_events`` uses. A line that is not valid
    JSON is masked as text. Private-key blocks are masked whole, header to
    footer (or to the end of the string when the footer is missing), which is
    more than ``longhand redact`` does locally: the local store keeps the key
    body, the copy that leaves the machine does not.
    """
    try:
        obj = json.loads(raw_json)
    except ValueError:
        text, keys = _mask_private_keys(raw_json)
        masked_text, _ = redact_text(text)
        text_counts = scan_text(text)
        if keys:
            text_counts[PRIVATE_KEY_PATTERN] = text_counts.get(PRIVATE_KEY_PATTERN, 0) + keys
        return masked_text, text_counts
    counts: dict[str, int] = {}
    masked_obj = _mask_obj(obj, counts)
    if not counts:
        return raw_json, {}
    return json.dumps(masked_obj), counts


# ─── Parts ────────────────────────────────────────────────────────────────────


@dataclass
class WrittenFiles:
    parts: list[str]
    lines: int
    bytes: int
    sha256: str


def _part_name(base_name: str, index: int) -> str:
    return f"{base_name}.part-{index:04d}.jsonl"


def write_session_files(
    lines: Iterable[str],
    dest_dir: Path,
    base_name: str,
    *,
    part_bytes: int = PART_BYTES_DEFAULT,
) -> WrittenFiles:
    """Write lines as ``<base>.jsonl``, or ``<base>.part-NNNN.jsonl`` parts.

    A part is closed on a line boundary when the next line would push it past
    ``part_bytes``; a single line longer than that becomes a part of its own.
    Bytes are UTF-8 with ``\\n`` line ends on every platform; the sha256 covers
    the concatenation of every part. Stale ``<base>*.jsonl`` files in
    ``dest_dir`` are removed first.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for stale in dest_dir.glob(f"{base_name}*.jsonl"):
        stale.unlink()

    digest = hashlib.sha256()
    parts: list[str] = []
    handle: IO[bytes] | None = None
    part_size = 0
    total_lines = 0
    total_bytes = 0

    def _open_next() -> IO[bytes]:
        name = _part_name(base_name, len(parts) + 1)
        parts.append(name)
        return (dest_dir / name).open("wb")

    try:
        for line in lines:
            data = line.encode("utf-8") + b"\n"
            if handle is None or (part_size > 0 and part_size + len(data) > part_bytes):
                if handle is not None:
                    handle.close()
                handle = _open_next()
                part_size = 0
            handle.write(data)
            digest.update(data)
            part_size += len(data)
            total_lines += 1
            total_bytes += len(data)
        if handle is None:
            handle = _open_next()
    finally:
        if handle is not None:
            handle.close()

    if len(parts) == 1:
        single = f"{base_name}.jsonl"
        (dest_dir / parts[0]).replace(dest_dir / single)
        parts = [single]
    return WrittenFiles(
        parts=parts, lines=total_lines, bytes=total_bytes, sha256=digest.hexdigest()
    )


def read_session_lines(session_dir: Path, meta: dict[str, Any]) -> Iterator[str]:
    """The exported lines of one session, parts in ``meta["parts"]`` order, no line ends."""
    for part in meta.get("parts") or []:
        with (session_dir / part).open("r", encoding="utf-8", newline="\n") as f:
            for line in f:
                yield line[:-1] if line.endswith("\n") else line


# ─── Export ───────────────────────────────────────────────────────────────────


@dataclass
class SessionExport:
    session_id: str
    source: str
    rel_dir: str
    files: WrittenFiles
    mask_counts: dict[str, int]
    meta: dict[str, Any]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write atomically: a killed run leaves the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload, indent=2, sort_keys=True))
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_session_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    _write_json(session_dir / SESSION_META_FILENAME, meta)


def read_session_json(session_dir: Path) -> dict[str, Any]:
    """The ``session.json`` of an exported session (raises when missing or corrupt)."""
    data = json.loads((session_dir / SESSION_META_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{session_dir / SESSION_META_FILENAME} is not a JSON object")
    return data


def export_session(
    sqlite: SQLiteStore,
    row: dict[str, Any],
    dest_root: Path,
    *,
    part_bytes: int = PART_BYTES_DEFAULT,
    mask: bool = True,
) -> SessionExport:
    """Write one session's transcript parts and ``session.json`` under ``dest_root``."""
    session_id = str(row["session_id"])
    source = session_source(session_id)
    rel_dir = session_rel_dir(row)
    session_dir = dest_root / Path(rel_dir)
    base_name = transcript_base_name(source)

    stream = _SessionLines(sqlite, session_id)
    mask_counts: dict[str, int] = {}

    def _lines() -> Iterator[str]:
        for raw in stream:
            if mask:
                raw, counts = mask_line(raw)
                for name, n in counts.items():
                    mask_counts[name] = mask_counts.get(name, 0) + n
            yield raw

    files = write_session_files(_lines(), session_dir, base_name, part_bytes=part_bytes)
    meta: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "longhand_version": __version__,
        "session_id": session_id,
        "source": source,
        "rel_dir": rel_dir,
        "parts": list(files.parts),
        "lines": files.lines,
        "bytes": files.bytes,
        "sha256": files.sha256,
        "event_count": row.get("event_count"),
        "last_event_timestamp": stream.last_timestamp,
        "exported_at": utcnow().isoformat(),
        "masked": bool(mask),
        "original_transcript_path": row.get("transcript_path"),
        "session": dict(row),
    }
    write_session_meta(session_dir, meta)
    return SessionExport(
        session_id=session_id,
        source=source,
        rel_dir=rel_dir,
        files=files,
        mask_counts=mask_counts,
        meta=meta,
    )


# ─── Repo-level files ─────────────────────────────────────────────────────────


def write_manifest(repo_root: Path, session_count: int) -> None:
    _write_json(
        repo_root / MANIFEST_FILENAME,
        {
            "format": BACKUP_FORMAT,
            "longhand_version": __version__,
            "updated_at": utcnow().isoformat(),
            "sessions": session_count,
        },
    )


def read_manifest(repo_root: Path) -> dict[str, Any] | None:
    return _read_json(repo_root / MANIFEST_FILENAME)


_GITIGNORE_LINES = (
    "# Longhand backup: working files a run may leave behind; never part of the backup",
    ".staging/",
    "*.merge/",
    "*.old/",
    ".DS_Store",
)


def write_gitignore_if_absent(repo_root: Path) -> None:
    """Create the repo's ``.gitignore``, or add any of our lines it lacks."""
    path = repo_root / GITIGNORE_FILENAME
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [line for line in _GITIGNORE_LINES if line not in existing]
    if existing and not missing:
        return
    repo_root.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if existing and existing[-1] != "":
            f.write("\n")
        for line in missing:
            f.write(line + "\n")


def write_readme_if_absent(repo_root: Path) -> None:
    path = repo_root / README_FILENAME
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(_README_TEXT)
