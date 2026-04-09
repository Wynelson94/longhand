"""
Deterministic time phrase parser.

Handles phrases like "yesterday", "last week", "a couple months ago",
"earlier today", "3 weeks ago", "this morning".

Returns (since, until) as tz-aware datetimes or (None, None) if no phrase
is found. Removes the matched phrase from the query.

No dateparser dependency. ~100 lines of rules.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day_start(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


# Phrase → (days_ago_start, days_ago_end) where end is more recent
_FIXED_PHRASES: list[tuple[re.Pattern[str], tuple[int, int]]] = [
    (re.compile(r"\bright now\b|\bcurrently\b", re.IGNORECASE), (1, 0)),
    (re.compile(r"\bearlier today\b|\bthis morning\b", re.IGNORECASE), (1, 0)),
    (re.compile(r"\btoday\b", re.IGNORECASE), (1, 0)),
    (re.compile(r"\byesterday\b", re.IGNORECASE), (2, 1)),
    (re.compile(r"\bthis week\b", re.IGNORECASE), (7, 0)),
    (re.compile(r"\blast week\b", re.IGNORECASE), (14, 7)),
    (re.compile(r"\ba couple weeks ago\b|\bcouple weeks ago\b", re.IGNORECASE), (21, 10)),
    (re.compile(r"\bthis month\b", re.IGNORECASE), (30, 0)),
    (re.compile(r"\blast month\b", re.IGNORECASE), (60, 30)),
    (re.compile(r"\ba couple months ago\b|\bcouple months ago\b", re.IGNORECASE), (90, 30)),
    (re.compile(r"\ba few months ago\b|\bfew months ago\b", re.IGNORECASE), (120, 30)),
    (re.compile(r"\blast year\b", re.IGNORECASE), (730, 365)),
    (re.compile(r"\bthis year\b", re.IGNORECASE), (365, 0)),
    (re.compile(r"\brecently\b|\blately\b", re.IGNORECASE), (14, 0)),
]

# Numeric phrases: "3 days ago", "2 weeks ago", "5 months ago"
_NUMERIC_PHRASE = re.compile(
    r"\b(\d+)\s+(day|week|month|year)s?\s+ago\b",
    re.IGNORECASE,
)

_UNIT_TO_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "year": 365,
}


def parse_time_phrase(
    query: str,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None, str]:
    """Parse time phrases out of a query.

    Returns (since, until, cleaned_query) where:
    - since/until are tz-aware UTC datetimes (or None if no phrase matched)
    - cleaned_query has the matched time phrase removed
    """
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        now = _utc(now)

    # Numeric phrase first (more specific)
    match = _NUMERIC_PHRASE.search(query)
    if match:
        n = int(match.group(1))
        unit = match.group(2).lower()
        days = n * _UNIT_TO_DAYS[unit]
        # Fuzzy window: ±25% around the target
        window = max(1, int(days * 0.25))
        target = now - timedelta(days=days)
        since = _day_start(target - timedelta(days=window))
        until = target + timedelta(days=window)
        cleaned = query[: match.start()] + query[match.end():]
        return since, until, cleaned.strip()

    # Fixed phrases
    for pattern, (days_start, days_end) in _FIXED_PHRASES:
        match = pattern.search(query)
        if match:
            since = _day_start(now - timedelta(days=days_start))
            until = now - timedelta(days=days_end) if days_end > 0 else now
            cleaned = query[: match.start()] + query[match.end():]
            return since, until, cleaned.strip()

    return None, None, query
