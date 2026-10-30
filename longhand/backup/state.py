"""Local state for the ``backup`` command: what the repo holds, as of the last run.

``<data_dir>/backup-state.json`` never enters the repo. Per session it keeps
the triple ``(event_count, last_event_timestamp, ingested_at)`` the store
showed when the session was last exported, plus the sha256 of the transcript
the repo holds. One ``GROUP BY`` over ``events`` finds the sessions whose
triple moved; only those are exported and hashed again. ``restore`` seeds the
same file, so a restored machine re-exports nothing until a session really
changes.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from longhand.storage.sqlite_store import SQLiteStore

STATE_FILENAME = "backup-state.json"

_MARKS_SQL = (
    "SELECT s.session_id, s.ingested_at, COUNT(e.rowid), MAX(e.timestamp) "
    "FROM sessions s JOIN events e ON e.session_id = s.session_id "
    "WHERE s.session_id != '' GROUP BY s.session_id"
)


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int_dict(value: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(key, str) and isinstance(val, int) and not isinstance(val, bool):
                out[key] = val
    return out


@dataclass
class SessionMark:
    """One session's store triple at export time, plus the sha256 the repo holds."""

    event_count: int
    last_event_timestamp: str | None
    ingested_at: str | None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMark:
        try:
            count = int(data.get("event_count") or 0)
        except (TypeError, ValueError):
            count = 0
        return cls(
            event_count=count,
            last_event_timestamp=_opt_str(data.get("last_event_timestamp")),
            ingested_at=_opt_str(data.get("ingested_at")),
            sha256=_opt_str(data.get("sha256")),
        )

    def changed_from(self, other: SessionMark) -> bool:
        """True when the store's triple moved. The sha is the repo's, not the store's."""
        return (
            self.event_count != other.event_count
            or self.last_event_timestamp != other.last_event_timestamp
            or self.ingested_at != other.ingested_at
        )


@dataclass
class BackupState:
    sessions: dict[str, SessionMark] = field(default_factory=dict)
    last_run_at: str | None = None
    last_push_at: str | None = None
    last_error: str | None = None
    last_mask_counts: dict[str, int] = field(default_factory=dict)
    last_run_summary: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, data_dir: Path) -> BackupState:
        """The saved state, or an empty one when the file is missing or unreadable."""
        try:
            data = json.loads((data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        sessions: dict[str, SessionMark] = {}
        raw_sessions = data.get("sessions")
        if isinstance(raw_sessions, dict):
            for sid, mark in raw_sessions.items():
                if isinstance(sid, str) and isinstance(mark, dict):
                    sessions[sid] = SessionMark.from_dict(mark)
        return cls(
            sessions=sessions,
            last_run_at=_opt_str(data.get("last_run_at")),
            last_push_at=_opt_str(data.get("last_push_at")),
            last_error=_opt_str(data.get("last_error")),
            last_mask_counts=_int_dict(data.get("last_mask_counts")),
            last_run_summary=_int_dict(data.get("last_run_summary")),
        )

    def save(self, data_dir: Path) -> None:
        """Write atomically (temp file, then replace), readable by the owner only."""
        payload = {
            "sessions": {sid: mark.to_dict() for sid, mark in sorted(self.sessions.items())},
            "last_run_at": self.last_run_at,
            "last_push_at": self.last_push_at,
            "last_error": self.last_error,
            "last_mask_counts": self.last_mask_counts,
            "last_run_summary": self.last_run_summary,
        }
        target = data_dir / STATE_FILENAME
        fd, tmp_name = tempfile.mkstemp(prefix=".backup-state-", suffix=".tmp", dir=str(data_dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def session_marks(sqlite: SQLiteStore) -> dict[str, SessionMark]:
    """The store's current triple for every session that has events (sha256 unset)."""
    marks: dict[str, SessionMark] = {}
    with sqlite.connect() as conn:
        for row in conn.execute(_MARKS_SQL):
            sid = row[0]
            if not isinstance(sid, str) or not sid:
                continue
            marks[sid] = SessionMark(
                event_count=int(row[2] or 0),
                last_event_timestamp=_opt_str(row[3]),
                ingested_at=_opt_str(row[1]),
            )
    return marks


def changed_sessions(current: dict[str, SessionMark], state: BackupState) -> list[str]:
    """Ids the store holds that the state does not, or whose triple moved; sorted."""
    return sorted(
        sid
        for sid, mark in current.items()
        if sid not in state.sessions or mark.changed_from(state.sessions[sid])
    )
