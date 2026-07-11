"""Tests for the deterministic time-phrase parser.

`parse_time_phrase` drives recall's date filtering, so a wrong window silently
degrades every "what did I do last week" query. It was previously exercised only
incidentally (the no-match return); these tests pin the actual phrase semantics.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


# ─── user-local day anchoring (v0.13) ─────────────────────────────────────────
#
# "today" means the user's calendar day. Anchoring day boundaries at UTC
# midnight cut local evenings off: at 5:30pm in UTC-7 the UTC date has already
# flipped, so a "today" query excluded everything the user did that day.

MST = timezone(timedelta(hours=-7))  # fixed offset, no DST — deterministic


def test_today_anchors_to_local_midnight_not_utc():
    # 2026-07-11 17:30 in UTC-7 == 2026-07-12 00:30 UTC: the UTC date flipped.
    local_evening = datetime(2026, 7, 11, 17, 30, tzinfo=MST)

    since, until, _ = parse_time_phrase("what did I do today", now=local_evening)

    # "today" = (1, 0): window starts at the LOCAL day-start of (now - 1d),
    # i.e. Jul 10 00:00-07:00 = Jul 10 07:00 UTC. The old UTC anchor placed
    # it at Jul 11 00:00 UTC — 5pm local the previous day — silently cutting
    # local mornings out of every "today" query for UTC-minus users.
    assert since == datetime(2026, 7, 10, 7, 0, tzinfo=timezone.utc)
    assert until == local_evening  # same instant
    assert since.utcoffset() == timedelta(0)  # edges are UTC-normalized
    assert until.utcoffset() == timedelta(0)


def test_yesterday_covers_the_local_calendar_day():
    local_evening = datetime(2026, 7, 11, 17, 30, tzinfo=MST)

    since, until, _ = parse_time_phrase("yesterday", now=local_evening)

    # (days_ago_start, days_ago_end) = (2, 1) anchored on the LOCAL day.
    assert since == datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
    assert until == datetime(2026, 7, 10, 17, 30, tzinfo=MST)


def test_tz_kwarg_anchors_a_naive_now():
    naive = datetime(2026, 7, 11, 17, 30)  # frozen wall clock, zone via tz=
    since, _, _ = parse_time_phrase("today", now=naive, tz=MST)
    assert since == datetime(2026, 7, 10, 7, 0, tzinfo=timezone.utc)


def test_default_anchor_is_local_and_aware():
    since, _, _ = parse_time_phrase("today")
    assert since is not None and since.tzinfo is not None
    # "today" = (1, 0): local day-start of (now - 1d), in the system zone.
    expected = (datetime.now().astimezone() - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert since == expected  # aware comparison — instant equality


def test_explicit_utc_now_keeps_utc_anchoring():
    """Regression: aware-UTC `now` (every existing caller) anchors in UTC."""
    since, until, _ = parse_time_phrase("yesterday", now=NOW)
    assert since == _dt(2026, 5, 26)
    assert until == _dt(2026, 5, 27, 12, 0)
