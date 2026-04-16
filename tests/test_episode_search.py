"""Tests for semantic episode search (R1 of the v0.5.9 recall-fidelity fix).

Covers:
  - new ChromaDB `episodes` collection is populated during ingest
  - `find_episodes` returns semantic matches for paraphrased queries
    that share no keywords with the episode text
  - backfill embeds existing SQLite rows idempotently
  - the recall pipeline no longer falls through to empty episodes on
    intent-framed queries
"""

from __future__ import annotations

from typing import Any

from longhand.recall.episode_search import find_episodes
from longhand.storage.store import LonghandStore, _build_episode_text


def _ingest_episode(store: LonghandStore, episode: dict[str, Any]) -> None:
    """Insert a single episode directly into SQLite + embed it.

    Bypasses the full ingest pipeline so the test can control episode
    content precisely.
    """
    store.sqlite.insert_episodes([episode])
    text = _build_episode_text(episode)
    store.vectors.add_episode_embedding(
        episode_id=episode["episode_id"],
        text=text,
        metadata={
            "session_id": episode.get("session_id") or "",
            "project_id": episode.get("project_id") or "",
            "ended_at": episode.get("ended_at") or "",
            "status": episode.get("status", "resolved"),
            "has_fix": bool(episode.get("fix_event_id")),
        },
    )


def _fixture_episode(
    *,
    episode_id: str,
    problem: str,
    fix: str,
    diagnosis: str = "",
    session_id: str = "test-sess",
    project_id: str | None = "test-proj",
) -> dict[str, Any]:
    return {
        "episode_id": episode_id,
        "session_id": session_id,
        "project_id": project_id,
        "started_at": "2026-04-01T00:00:00+00:00",
        "ended_at": "2026-04-01T01:00:00+00:00",
        "problem_event_id": f"{episode_id}-prob",
        "diagnosis_event_id": f"{episode_id}-diag" if diagnosis else None,
        "fix_event_id": f"{episode_id}-fix",
        "verification_event_id": None,
        "problem_description": problem,
        "diagnosis_summary": diagnosis,
        "fix_summary": fix,
        "touched_files": [],
        "tags": [],
        "confidence": 0.8,
        "status": "resolved",
    }


# ─── Collection setup ───────────────────────────────────────────────────────


def test_episodes_collection_exists(temp_store: LonghandStore):
    """VectorStore must initialize the episodes collection alongside the others."""
    assert hasattr(temp_store.vectors, "episodes_collection")
    assert temp_store.vectors.episode_count() == 0


def test_add_episode_embedding_upserts(temp_store: LonghandStore):
    """add_episode_embedding writes into the collection and upserts on rerun."""
    temp_store.vectors.add_episode_embedding(
        episode_id="ep-1",
        text="Problem: X. Fix: Y.",
        metadata={"session_id": "s", "project_id": "p", "ended_at": "", "status": "resolved", "has_fix": True},
    )
    assert temp_store.vectors.episode_count() == 1

    # Upsert same id — count should not grow
    temp_store.vectors.add_episode_embedding(
        episode_id="ep-1",
        text="Problem: X. Fix: Y (updated).",
        metadata={"session_id": "s", "project_id": "p", "ended_at": "", "status": "resolved", "has_fix": True},
    )
    assert temp_store.vectors.episode_count() == 1


# ─── Semantic retrieval (the headline behavior) ─────────────────────────────


def test_find_episodes_matches_paraphrase(temp_store: LonghandStore):
    """find_episodes retrieves an episode whose text shares no keywords
    with the query — the whole point of R1.
    """
    _ingest_episode(
        temp_store,
        _fixture_episode(
            episode_id="ep-auth",
            problem="Users can't log in after the session token stopped refreshing automatically",
            diagnosis="Cookie SameSite setting was too restrictive for the callback domain",
            fix="Set SameSite=None and Secure on the auth cookie in middleware",
        ),
    )
    _ingest_episode(
        temp_store,
        _fixture_episode(
            episode_id="ep-pagination",
            problem="Table of products loads slowly when the list grows past five hundred rows",
            fix="Added a LIMIT/OFFSET pagination query with indexed cursor column",
        ),
    )

    # Paraphrase: no literal overlap with "token stopped refreshing"
    hits = find_episodes(
        temp_store,
        query="people are getting signed out unexpectedly",
        limit=5,
    )
    assert hits, "expected at least one semantic match"
    assert hits[0]["episode_id"] == "ep-auth", (
        f"expected auth episode to rank first, got {hits[0]['episode_id']}"
    )


def test_find_episodes_ranks_by_distance(temp_store: LonghandStore):
    """When two episodes match, the semantically closer one ranks first."""
    _ingest_episode(
        temp_store,
        _fixture_episode(
            episode_id="ep-close",
            problem="Deployment pipeline fails intermittently during the artifact upload step",
            fix="Added retry with exponential backoff to the S3 upload call",
        ),
    )
    _ingest_episode(
        temp_store,
        _fixture_episode(
            episode_id="ep-far",
            problem="Form validation accepts empty email addresses on the signup page",
            fix="Added zod schema requiring email() on the signup form",
        ),
    )

    hits = find_episodes(
        temp_store,
        query="CI keeps failing when pushing release artifacts",
        limit=5,
    )
    assert hits[0]["episode_id"] == "ep-close"
    assert "_distance" in hits[0]


def test_find_episodes_falls_back_when_vectors_empty(temp_store: LonghandStore):
    """If the episodes vector collection is empty, fall back to SQL so the
    caller still sees SOMETHING instead of a silent void.
    """
    # Insert into SQLite only — do NOT embed
    temp_store.sqlite.insert_episodes([
        _fixture_episode(
            episode_id="ep-sql-only",
            problem="Some problem text",
            fix="Some fix text",
        )
    ])

    hits = find_episodes(temp_store, query="anything at all", limit=5)
    assert hits, "vector-empty path should fall back to SQL, not return []"
    assert hits[0]["episode_id"] == "ep-sql-only"


def test_find_episodes_has_fix_filter(temp_store: LonghandStore):
    """has_fix=True hides unresolved episodes."""
    unresolved = _fixture_episode(
        episode_id="ep-unresolved",
        problem="Still investigating the flaky websocket reconnection behavior",
        fix="",
    )
    unresolved["fix_event_id"] = None
    unresolved["status"] = "unresolved"
    _ingest_episode(temp_store, unresolved)

    _ingest_episode(
        temp_store,
        _fixture_episode(
            episode_id="ep-resolved",
            problem="Websocket reconnection was dropping messages during a deploy",
            fix="Added buffered replay with client-side message IDs",
        ),
    )

    hits = find_episodes(
        temp_store,
        query="websocket reliability problem",
        has_fix=True,
        limit=5,
    )
    assert all(h.get("fix_event_id") for h in hits)
    assert "ep-unresolved" not in {h["episode_id"] for h in hits}


# ─── Backfill ───────────────────────────────────────────────────────────────


def test_backfill_embeds_existing_sqlite_rows(temp_store: LonghandStore):
    """backfill_episode_embeddings copies every SQLite episode row into vectors."""
    # Seed three episodes into SQLite only
    for i in range(3):
        temp_store.sqlite.insert_episodes([
            _fixture_episode(
                episode_id=f"ep-back-{i}",
                problem=f"Problem number {i}",
                fix=f"Fix number {i}",
            )
        ])

    assert temp_store.vectors.episode_count() == 0

    n = temp_store.backfill_episode_embeddings()
    assert n == 3
    assert temp_store.vectors.episode_count() == 3


def test_backfill_is_idempotent(temp_store: LonghandStore):
    """Running backfill twice produces the same vector count (upsert, not duplicate)."""
    temp_store.sqlite.insert_episodes([
        _fixture_episode(
            episode_id="ep-idem",
            problem="Some problem",
            fix="Some fix",
        )
    ])
    temp_store.backfill_episode_embeddings()
    temp_store.backfill_episode_embeddings()
    assert temp_store.vectors.episode_count() == 1


def test_ensure_episode_embeddings_triggers_on_empty_collection(temp_store: LonghandStore):
    """ensure_episode_embeddings auto-backfills when vectors are empty."""
    temp_store.sqlite.insert_episodes([
        _fixture_episode(
            episode_id="ep-auto",
            problem="Some problem",
            fix="Some fix",
        )
    ])

    n = temp_store.ensure_episode_embeddings()
    assert n == 1
    assert temp_store.vectors.episode_count() == 1


def test_ensure_episode_embeddings_noops_when_already_populated(temp_store: LonghandStore):
    """ensure_episode_embeddings returns 0 once embeddings exist."""
    _ingest_episode(
        temp_store,
        _fixture_episode(
            episode_id="ep-noop",
            problem="Some problem",
            fix="Some fix",
        ),
    )

    n = temp_store.ensure_episode_embeddings()
    assert n == 0
