"""Shared CLI helpers — console instance, store factory, prefix resolution,
and timestamp formatting. Kept module-level so tests can import them directly
without pulling in the full command surface (which registers decorators at
import time and is expensive).
"""

from __future__ import annotations

from datetime import datetime

from rich.console import Console

from longhand.storage import LonghandStore

console = Console()


def _get_store(data_dir: str | None = None) -> LonghandStore:
    return LonghandStore(data_dir=data_dir)


def _resolve_prefix(store: LonghandStore, prefix: str) -> str | None:
    """Resolve a session ID prefix to a full session ID.

    SQL LIKE over the whole table — the old approach scanned only the 1,000
    most recent sessions in Python, so prefixes of older sessions silently
    failed to resolve. Most-recent match wins on ambiguity (unchanged).
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with store.sqlite.connect() as conn:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ? ESCAPE '\\'"
            " ORDER BY started_at DESC LIMIT 1",
            (escaped + "%",),
        ).fetchone()
    return row[0] if row else None


def _format_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]
