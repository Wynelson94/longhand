"""Tests for the deterministic time-phrase parser.

`parse_time_phrase` drives recall's date filtering, so a wrong window silently
degrades every "what did I do last week" query. It was previously exercised only
incidentally (the no-match return); these tests pin the actual phrase semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from longhand.recall.time_parser import parse_time_phrase

# A fixed "now" so every window is deterministic.
NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


def _dt(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# Phrases that must produce a (since, until) window. The phrase text must also
# be stripped out of the returned cleaned query.
MATCHING = [
    "today",
    "yesterday",
    "earlier today",
    "this morning",
    "right now",
    "this week",
    "last week",
    "a couple weeks ago",
    "this month",
    "last month",
    "a couple months ago",
    "a few months ago",
    "this year",
    "last year",
    "recently",
    "3 days ago",
    "2 weeks ago",
    "5 months ago",
    "1 year ago",
]


@pytest.mark.parametrize("phrase", MATCHING)
def test_phrase_produces_ordered_window_and_is_stripped(phrase):
    query = f"the auth bug from {phrase} that broke login"
    since, until, cleaned = parse_time_phrase(query, now=NOW)

    assert since is not None and until is not None, f"{phrase!r} should match"
    assert since <= until, f"{phrase!r} produced an inverted window"
    assert until <= NOW, f"{phrase!r} window extends into the future"
    # The matched phrase is removed; the surrounding words survive.
    assert phrase not in cleaned, f"{phrase!r} not stripped from cleaned query"
    assert "auth bug" in cleaned and "broke login" in cleaned


@pytest.mark.parametrize(
    "query",
    [
        "where did I put the readme",
        "fix the failing test",
        "",
        "summary of the deployment",
    ],
)
def test_no_time_phrase_returns_none_and_unchanged_query(query):
    since, until, cleaned = parse_time_phrase(query, now=NOW)
    assert since is None
    assert until is None
    assert cleaned == query


def test_yesterday_exact_window():
    since, until, _ = parse_time_phrase("what about yesterday", now=NOW)
    # (days_ago_start, days_ago_end) = (2, 1): since = start of 2 days ago,
    # until = exactly 1 day before now.
    assert since == _dt(2026, 5, 26)
    assert until == _dt(2026, 5, 27, 12, 0)


def test_today_window_runs_up_to_now():
    since, until, _ = parse_time_phrase("anything today", now=NOW)
    assert since == _dt(2026, 5, 27)  # start of (now - 1 day)
    assert until == NOW  # days_end == 0 → until is now


def test_numeric_phrase_brackets_the_target_day():
    # "3 days ago": target = now - 3d, fuzzy window = max(1, int(3*0.25)) = 1 day.
    since, until, cleaned = parse_time_phrase("the bug 3 days ago here", now=NOW)
    assert since == _dt(2026, 5, 24)  # day-start of (target - 1d)
    assert until == _dt(2026, 5, 26, 12, 0)  # target + 1d
    assert "3 days ago" not in cleaned
    assert "the bug" in cleaned and "here" in cleaned


def test_numeric_phrase_takes_precedence_over_fixed():
    # "2 weeks ago" (numeric) should win over the bare "week" fixed phrases.
    since, until, _ = parse_time_phrase("2 weeks ago", now=NOW)
    # 14 days back, ±25% → window = 3 days.
    assert since == _dt(2026, 5, 11)  # day-start of (now - 14d - 3d)
    assert until == _dt(2026, 5, 17, 12, 0)  # (now - 14d) + 3d


def test_naive_now_is_treated_as_utc():
    naive = datetime(2026, 5, 28, 12, 0, 0)  # no tzinfo
    since, until, _ = parse_time_phrase("yesterday", now=naive)
    assert since is not None and until is not None
    assert since.tzinfo is not None and until.tzinfo is not None


def test_default_now_uses_current_time():
    # Exercises the `now is None` default branch (falls back to datetime.now).
    since, until, cleaned = parse_time_phrase("the deploy yesterday")
    assert since is not None and until is not None
    assert since <= until
    assert "yesterday" not in cleaned
