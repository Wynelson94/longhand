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
    """Resolve a session ID prefix to a full session ID."""
    rows = store.sqlite.list_sessions(limit=1000)
    for row in rows:
        if row["session_id"].startswith(prefix):
            return row["session_id"]
    return None


def _format_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso[:16]
