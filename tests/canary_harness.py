"""Harness for Layer 1 canary corpus tests.

Each canary fixture is a Python module under `tests/fixtures/corpus/` that
exposes:

    SESSIONS: list[tuple[str, list[dict]]]
        (filename, [event_dicts]) — one tuple per session JSONL to ingest.

    ASSERTIONS: list[RecallAssertion | OutputAssertion]
        Each assertion runs against the populated store.

    DESCRIPTION: str
        One-paragraph description of the bug class this canary captures.

The harness writes each session to disk, ingests it into a fresh store, then
runs every assertion. Pytest collects all canaries via parametrize so each
canary becomes its own test ID.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from longhand.parser import JSONLParser
from longhand.recall.recall_pipeline import RecallResult, recall, recall_project_status
from longhand.storage.store import LonghandStore


@dataclass
class ProjectStatusAssertion:
    """Assert `recall_project_status(project)` finds the project and links sessions.

    The bug class this catches: project_id ends up NULL on the session row,
    so the project looks like it has zero history even when transcripts and
    events exist. User-facing symptom is the dreaded "No session history
    found" message despite the corpus clearly containing the work.
    """

    project_query: str
    min_indexed_sessions: int = 1
    description: str = ""

    def check(self, store: LonghandStore) -> None:
        label = self.description or self.project_query
        status = recall_project_status(store, self.project_query)
        assert status is not None, (
            f"{label!r}: recall_project_status({self.project_query!r}) returned "
            "None — project not matched at all"
        )
        assert status.session_count_indexed >= self.min_indexed_sessions, (
            f"{label!r}: project {self.project_query!r} matched but only "
            f"{status.session_count_indexed} sessions indexed "
            f"(expected ≥{self.min_indexed_sessions}). "
            f"This is the 'No session history found' bug class."
        )


@dataclass
class RecallAssertion:
    """Assert that running `recall(query)` surfaces a specific session.

    `must_appear_in` controls WHERE the session must appear. The default is
    "narrative" because that is what users (and Claude) actually read — a
    session that lives in `result.segments` but never makes it into the
    rendered markdown is not surfaced from the user's perspective.

      - "narrative"   — session_id prefix appears in result.narrative (default)
      - "episodes"    — episode-list hit (internal data path)
      - "segments"    — segment-list hit (internal data path)
      - "footer"      — specifically inside the "Also possibly relevant" footer
      - "any_data"    — episodes, segments, OR narrative (loosest, debug only)
    """

    query: str
    must_surface_session_id: str
    must_appear_in: str = "narrative"
    description: str = ""

    def check(self, store: LonghandStore) -> None:
        result = recall(store, self.query)
        sid = self.must_surface_session_id
        label = self.description or self.query
        if self.must_appear_in == "narrative":
            assert _in_narrative(result, sid), (
                f"{label!r}: session prefix {sid[:8]!r} not in rendered narrative. "
                f"Internal: episodes={[e.get('session_id') for e in result.episodes]}, "
                f"segments={[s.get('session_id') for s in result.segments]}. "
                f"Narrative was:\n{result.narrative}"
            )
        elif self.must_appear_in == "episodes":
            assert _in_episodes(result, sid), (
                f"{label!r}: session {sid} not in episodes. "
                f"Got: {[e.get('session_id') for e in result.episodes]}"
            )
        elif self.must_appear_in == "segments":
            assert _in_segments(result, sid), (
                f"{label!r}: session {sid} not in segments. "
                f"Got: {[s.get('session_id') for s in result.segments]}"
            )
        elif self.must_appear_in == "footer":
            assert _in_narrative_footer(result, sid), (
                f"{label!r}: session prefix {sid[:8]!r} not in narrative footer. "
                f"Narrative was:\n{result.narrative}"
            )
        elif self.must_appear_in == "any_data":
            assert (
                _in_episodes(result, sid)
                or _in_segments(result, sid)
                or _in_narrative(result, sid)
            ), (
                f"{label!r}: session {sid} not surfaced anywhere. "
                f"Episodes: {[e.get('session_id') for e in result.episodes]}, "
                f"Segments: {[s.get('session_id') for s in result.segments]}, "
                f"Narrative excerpt:\n{result.narrative[:500]}"
            )
        else:
            raise ValueError(f"unknown must_appear_in: {self.must_appear_in}")


@dataclass
class OutputAssertion:
    """Assert a predicate over the store's episodes/segments/etc.

    `predicate` takes the store and returns (ok: bool, detail: str).
    """

    description: str
    predicate: Callable[[LonghandStore], tuple[bool, str]]

    def check(self, store: LonghandStore) -> None:
        ok, detail = self.predicate(store)
        assert ok, f"{self.description}: {detail}"


def _in_episodes(result: RecallResult, session_id: str) -> bool:
    return any(ep.get("session_id") == session_id for ep in result.episodes)


def _in_segments(result: RecallResult, session_id: str) -> bool:
    return any(seg.get("session_id") == session_id for seg in result.segments)


def _in_narrative_footer(result: RecallResult, session_id: str) -> bool:
    return session_id[:8] in result.narrative and "Also possibly relevant" in result.narrative


def _in_narrative(result: RecallResult, session_id: str) -> bool:
    return session_id[:8] in result.narrative


def write_and_ingest(store: LonghandStore, dir_: Path, sessions: list[tuple[str, list[dict[str, Any]]]]) -> None:
    """Write each session to disk under dir_ and ingest into store."""
    for filename, events in sessions:
        path = dir_ / filename
        with path.open("w") as f:
            for event in events:
                f.write(json.dumps(event) + "\n")
        parser = JSONLParser(path)
        parsed_events = list(parser.parse_events())
        session = parser.build_session(parsed_events)
        store.ingest_session(session, parsed_events)


def discover_canaries() -> list[str]:
    """Return module names of all canary fixtures."""
    from tests.fixtures import corpus as corpus_pkg

    names: list[str] = []
    for info in pkgutil.iter_modules(corpus_pkg.__path__):
        if info.name.startswith("canary_"):
            names.append(f"tests.fixtures.corpus.{info.name}")
    return sorted(names)


def load_canary(module_name: str):
    """Import a canary module and return it."""
    return importlib.import_module(module_name)
