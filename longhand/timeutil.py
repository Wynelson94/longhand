"""Shared timezone-aware clock helpers.

Storage timestamps are UTC ISO-8601 with an explicit offset (+00:00).
Rows written before v0.13 are naive local-clock strings; readers interpret
naive values as UTC — metadata-grade tolerance, no backfill migration.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """The current moment as a tz-aware UTC datetime."""
    return datetime.now(timezone.utc)
