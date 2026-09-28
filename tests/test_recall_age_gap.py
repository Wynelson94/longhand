"""Issue #82: say so when the best match is much older than the alternatives.

Recency is nearly weightless in episode ranking (at most 0.5 points against 10
per keyword hit), so an old episode can outrank fresh work on the same topic
and read as current. Re-weighting is the wrong fix: "what did I do last year"
needs old results to win. So recall does not reorder anything. When a query
carries no time phrase and the top episode is much older than the newest
runner-up, it names the gap instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from longhand.recall import recall_pipeline
from longhand.recall.narrative import build_narrative
from longhand.recall.recall_pipeline import _age_gap_note, recall

NOW = datetime.now(timezone.utc)


def _ep(episode_id: str, days_ago: int | None, **extra) -> dict:
    stamp = (NOW - timedelta(days=days_ago)).isoformat() if days_ago is not None else None
    return {"episode_id": episode_id, "started_at": stamp, "ended_at": stamp, **extra}


# ─── the helper ────────────────────────────────────────────────────────────


def test_flags_an_old_top_match_that_has_fresh_runner_ups():
    note = _age_gap_note([_ep("old", 120), _ep("fresh", 2)], NOW, time_scoped=False)
    assert note is not None
    assert "4 months ago" in note
    assert "2 days ago" in note


def test_silent_when_the_query_carries_a_time_phrase():
    """'what did I do last year' asked for old results; no warning."""
    assert _age_gap_note([_ep("old", 120), _ep("fresh", 2)], NOW, time_scoped=True) is None


def test_silent_with_a_single_episode():
    assert _age_gap_note([_ep("only", 400)], NOW, time_scoped=False) is None


@pytest.mark.parametrize(
    ("top_days", "runner_up_days"),
    [
        (20, 0),  # top is under a month old: recent enough to read as current
        (100, 80),  # both old; a 20-day gap is not "much older"
        (2, 200),  # the top IS the newest — the normal, healthy case
        (45, 44),  # same era, e.g. two episodes from one long session
    ],
)
def test_silent_below_the_thresholds(top_days, runner_up_days):
    episodes = [_ep("top", top_days), _ep("runner_up", runner_up_days)]
    assert _age_gap_note(episodes, NOW, time_scoped=False) is None


def test_measures_the_gap_to_the_newest_runner_up_not_the_first():
    note = _age_gap_note(
        [_ep("old", 120), _ep("middle", 90), _ep("fresh", 3)], NOW, time_scoped=False
    )
    assert note is not None
    assert "3 days ago" in note


def test_missing_or_malformed_timestamps_never_raise():
    garbage = {"episode_id": "bad", "started_at": "not-a-date", "ended_at": None}
    assert _age_gap_note([_ep("a", None), _ep("b", 2)], NOW, time_scoped=False) is None
    assert _age_gap_note([_ep("old", 120), garbage], NOW, time_scoped=False) is None
    assert _age_gap_note([garbage, _ep("fresh", 2)], NOW, time_scoped=False) is None


def test_falls_back_to_ended_at_when_started_at_is_missing():
    top = {
        "episode_id": "old",
        "started_at": None,
        "ended_at": (NOW - timedelta(days=120)).isoformat(),
    }
    assert _age_gap_note([top, _ep("fresh", 2)], NOW, time_scoped=False) is not None


def test_does_not_reorder_or_mutate_the_episodes():
    episodes = [_ep("old", 120), _ep("fresh", 2)]
    before = [dict(e) for e in episodes]
    _age_gap_note(episodes, NOW, time_scoped=False)
    assert episodes == before


# ─── the narrative ─────────────────────────────────────────────────────────


def test_narrative_states_the_note_before_the_answer():
    episodes = [
        _ep("old", 120, session_id="aaaaaaaa1111", problem_description="login fails on refresh"),
        _ep("fresh", 2, session_id="bbbbbbbb2222", problem_description="login fails again"),
    ]
    narrative = build_narrative(
        query="the login bug",
        project_matches=[],
        episodes=episodes,
        artifacts={},
        age_gap_note="AGE-GAP-NOTE",
    )
    assert "AGE-GAP-NOTE" in narrative
    assert narrative.index("AGE-GAP-NOTE") < narrative.index("### What went wrong")


def test_narrative_has_no_note_when_there_is_none():
    episodes = [_ep("old", 120, session_id="aaaaaaaa1111", problem_description="login fails")]
    narrative = build_narrative(
        query="the login bug", project_matches=[], episodes=episodes, artifacts={}
    )
    assert "Older than the other matches" not in narrative


# ─── end to end through recall() ───────────────────────────────────────────


def _stub_search(monkeypatch, episodes: list[dict]) -> None:
    """Hand recall() a fixed candidate list; ranking and gating stay real."""
    monkeypatch.setattr(recall_pipeline, "find_episodes", lambda **_: [dict(e) for e in episodes])
    monkeypatch.setattr(recall_pipeline, "find_segments", lambda **_: [])


def _candidates() -> list[dict]:
    # The old episode wins on semantic distance, as in issue #82's measurement.
    return [
        _ep(
            "ep_old",
            150,
            session_id="0ld0ld0ld0ld",
            _distance=0.2,
            confidence=0.8,
            problem_description="login fails after token refresh",
        ),
        _ep(
            "ep_fresh",
            1,
            session_id="fre5hfre5h00",
            _distance=0.6,
            confidence=0.8,
            problem_description="login fails after token refresh",
        ),
    ]


def test_recall_flags_the_gap_without_reordering(temp_store, monkeypatch):
    _stub_search(monkeypatch, _candidates())
    result = recall(temp_store, "login token refresh bug", now=NOW)

    assert [e["episode_id"] for e in result.episodes] == ["ep_old", "ep_fresh"]
    assert result.age_gap_note is not None
    assert "5 months ago" in result.age_gap_note
    assert result.age_gap_note in result.narrative


def test_recall_with_a_time_phrase_carries_no_note(temp_store, monkeypatch):
    _stub_search(monkeypatch, _candidates())
    result = recall(temp_store, "login token refresh bug last year", now=NOW)

    assert [e["episode_id"] for e in result.episodes] == ["ep_old", "ep_fresh"]
    assert result.age_gap_note is None
