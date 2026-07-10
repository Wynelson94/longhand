"""
ChromaDB-backed vector storage for semantic search over event content.

Events are embedded for semantic retrieval, but the authoritative
record lives in SQLite — the vector store only holds what's needed
for search (event_id, truncated content, filter metadata).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from longhand.types import Event, EventType

# Limit embedded text length to keep Chroma performant.
# The full content is always retrievable from SQLite by event_id.
MAX_EMBED_CHARS = 2000

# Per-upsert batch size for Chroma. Chroma tolerates larger batches but 500
# is the stable sweet spot: enough to amortize ONNX call overhead, small
# enough to avoid memory spikes on low-RAM systems.
CHROMA_BATCH_SIZE = 500


def _first_batch(rows: Any) -> list[Any]:
    """Unwrap Chroma's per-query result lists, which may be None or empty."""
    return rows[0] if rows else []


class VectorStore:
    """ChromaDB wrapper for semantic search over Longhand events."""

    def __init__(self, persist_dir: str | Path):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # Silence Chroma telemetry
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )

        self.events_collection = self.client.get_or_create_collection(
            name="events",
            metadata={"description": "All session events, embedded for semantic search"},
        )

        self.sessions_collection = self.client.get_or_create_collection(
            name="sessions",
            metadata={"description": "One embedding per session for fuzzy recall (v0.2)"},
        )

        self.projects_collection = self.client.get_or_create_collection(
            name="projects",
            metadata={"description": "One embedding per project for fuzzy project matching"},
        )

        self.segments_collection = self.client.get_or_create_collection(
            name="segments",
            metadata={
                "description": "One embedding per conversation segment for topic-level recall"
            },
        )

        self.episodes_collection = self.client.get_or_create_collection(
            name="episodes",
            metadata={
                "description": "One embedding per problem→fix episode for intent-framed recall"
            },
        )

    def add_events(self, events: list[Event]) -> int:
        """Add a batch of events to the vector index.

        Duplicate event_ids are overwritten (upsert).
        """
        if not events:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Any] = []

        for e in events:
            if not e.content or not e.content.strip():
                continue

            text = e.content[:MAX_EMBED_CHARS]
            meta = {
                "session_id": e.session_id,
                "event_type": e.event_type if isinstance(e.event_type, str) else e.event_type.value,
                "sequence": e.sequence,
                "timestamp": e.timestamp.isoformat(),
            }
            if e.tool_name:
                meta["tool_name"] = e.tool_name
            if e.file_path:
                meta["file_path"] = e.file_path
            if e.model:
                meta["model"] = e.model

            ids.append(e.event_id)
            documents.append(text)
            metadatas.append(meta)

        if not ids:
            return 0

        # Chunk into batches of CHROMA_BATCH_SIZE for Chroma stability.
        added = 0
        for i in range(0, len(ids), CHROMA_BATCH_SIZE):
            batch_ids = ids[i : i + CHROMA_BATCH_SIZE]
            batch_docs = documents[i : i + CHROMA_BATCH_SIZE]
            batch_meta = metadatas[i : i + CHROMA_BATCH_SIZE]
            self.events_collection.upsert(
                ids=batch_ids,
                documents=batch_docs,
                metadatas=batch_meta,
            )
            added += len(batch_ids)

        return added

    def search(
        self,
        query: str,
        n_results: int = 10,
        event_type: str | EventType | None = None,
        session_id: str | None = None,
        tool_name: str | None = None,
        file_path_contains: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search with optional structured filters."""
        where_clauses: list[dict[str, Any]] = []
        if event_type:
            etype = event_type if isinstance(event_type, str) else event_type.value
            where_clauses.append({"event_type": etype})
        if session_id:
            where_clauses.append({"session_id": session_id})
        if tool_name:
            where_clauses.append({"tool_name": tool_name})

        where: dict[str, Any] | None = None
        if len(where_clauses) == 1:
            where = where_clauses[0]
        elif len(where_clauses) > 1:
            where = {"$and": where_clauses}

        try:
            results = self.events_collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
        except Exception:
            # Empty collection or query failure — return no results gracefully
            return []

        ids = _first_batch(results.get("ids"))
        documents = _first_batch(results.get("documents"))
        metadatas = _first_batch(results.get("metadatas"))
        distances = _first_batch(results.get("distances"))

        hits: list[dict[str, Any]] = []
        for i, event_id in enumerate(ids):
            hits.append(
                {
                    "event_id": event_id,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 1.0,
                }
            )

        # Apply file_path_contains as post-filter (Chroma doesn't do LIKE)
        if file_path_contains:
            hits = [
                h
                for h in hits
                if file_path_contains.lower() in (h["metadata"].get("file_path") or "").lower()
            ]

        return hits

    def count(self) -> int:
        return self.events_collection.count()

    def redact_documents(self, redactor: Callable[[str], tuple[str, int]]) -> int:
        """Apply a text redactor to every stored document, re-embedding changed ones.

        Used by `longhand redact --apply` to retroactively mask secrets in
        documents embedded before redaction was enabled. Returns the number
        of documents updated. The redactor must never log raw values.
        """
        changed = 0
        collections = (
            self.events_collection,
            self.sessions_collection,
            self.projects_collection,
            self.segments_collection,
            self.episodes_collection,
        )
        for collection in collections:
            try:
                total = collection.count()
            except Exception:
                continue
            offset = 0
            while offset < total:
                batch = collection.get(
                    limit=CHROMA_BATCH_SIZE,
                    offset=offset,
                    # Chroma accepts plain strings at runtime; the stubs in
                    # some versions want IncludeEnum, which 0.5.x lacks.
                    include=["documents"],  # type: ignore[list-item]
                )
                ids = batch.get("ids") or []
                docs = batch.get("documents") or []
                upd_ids: list[str] = []
                upd_docs: list[str] = []
                for doc_id, doc in zip(ids, docs, strict=False):
                    if not doc:
                        continue
                    new_doc, n = redactor(doc)
                    if n:
                        upd_ids.append(doc_id)
                        upd_docs.append(new_doc)
                if upd_ids:
                    collection.update(ids=upd_ids, documents=upd_docs)
                    changed += len(upd_ids)
                offset += CHROMA_BATCH_SIZE
        return changed

    # ─── Sessions collection (v0.2 proactive memory) ───────────────────────

    def add_session_embedding(
        self,
        session_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Upsert one embedding per session for fuzzy recall."""
        if not text or not text.strip():
            return
        self.sessions_collection.upsert(
            ids=[session_id],
            documents=[text[:MAX_EMBED_CHARS]],
            metadatas=[metadata],
        )

    def search_sessions(
        self,
        query: str,
        n_results: int = 10,
        project_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fuzzy session search with optional project/time filters."""
        where_clauses: list[dict[str, Any]] = []
        if project_id:
            where_clauses.append({"project_id": project_id})
        if since:
            where_clauses.append({"started_at": {"$gte": since}})
        if until:
            where_clauses.append({"started_at": {"$lte": until}})

        where: dict[str, Any] | None = None
        if len(where_clauses) == 1:
            where = where_clauses[0]
        elif len(where_clauses) > 1:
            where = {"$and": where_clauses}

        try:
            results = self.sessions_collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
        except Exception:
            return []

        ids = _first_batch(results.get("ids"))
        documents = _first_batch(results.get("documents"))
        metadatas = _first_batch(results.get("metadatas"))
        distances = _first_batch(results.get("distances"))

        hits: list[dict[str, Any]] = []
        for i, sid in enumerate(ids):
            hits.append(
                {
                    "session_id": sid,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 1.0,
                }
            )
        return hits

    # ─── Projects collection ───────────────────────────────────────────────

    def add_project_embedding(
        self,
        project_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Upsert one embedding per project (keyword blob + category + name)."""
        if not text or not text.strip():
            return
        self.projects_collection.upsert(
            ids=[project_id],
            documents=[text[:MAX_EMBED_CHARS]],
            metadatas=[metadata],
        )

    def search_projects(
        self,
        query: str,
        n_results: int = 10,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search over project descriptions."""
        where: dict[str, Any] | None = {"category": category} if category else None

        try:
            results = self.projects_collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
        except Exception:
            return []

        ids = _first_batch(results.get("ids"))
        documents = _first_batch(results.get("documents"))
        metadatas = _first_batch(results.get("metadatas"))
        distances = _first_batch(results.get("distances"))

        hits: list[dict[str, Any]] = []
        for i, pid in enumerate(ids):
            hits.append(
                {
                    "project_id": pid,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 1.0,
                }
            )
        return hits

    # ─── Segments collection ─────────────────────────────────────────────

    def add_segment_embedding(
        self,
        segment_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Upsert one embedding per conversation segment."""
        if not text or not text.strip():
            return
        self.segments_collection.upsert(
            ids=[segment_id],
            documents=[text[:MAX_EMBED_CHARS]],
            metadatas=[metadata],
        )

    def add_segment_embeddings_batch(self, items: list[dict[str, Any]]) -> int:
        """Upsert multiple segment embeddings in one ONNX batch.

        Each item: ``{"segment_id": str, "text": str, "metadata": dict}``.
        Items with empty or whitespace-only text are dropped, and duplicate
        ids keep only the first occurrence — Chroma rejects a batch wholesale
        when it contains a repeated id. Chunked at CHROMA_BATCH_SIZE for
        Chroma stability. Returns the number of embeddings upserted.
        """
        if not items:
            return 0

        seen_ids: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in items:
            sid = str(item.get("segment_id") or "")
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            deduped.append(item)
        items = deduped

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Any] = []
        for item in items:
            text = item.get("text") or ""
            if not text.strip():
                continue
            ids.append(item["segment_id"])
            documents.append(text[:MAX_EMBED_CHARS])
            metadatas.append(item["metadata"])

        if not ids:
            return 0

        added = 0
        for i in range(0, len(ids), CHROMA_BATCH_SIZE):
            self.segments_collection.upsert(
                ids=ids[i : i + CHROMA_BATCH_SIZE],
                documents=documents[i : i + CHROMA_BATCH_SIZE],
                metadatas=metadatas[i : i + CHROMA_BATCH_SIZE],
            )
            added += len(ids[i : i + CHROMA_BATCH_SIZE])
        return added

    def search_segments(
        self,
        query: str,
        n_results: int = 10,
        project_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        segment_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search over conversation segments."""
        where_clauses: list[dict[str, Any]] = []
        if project_id:
            where_clauses.append({"project_id": project_id})
        if segment_type:
            where_clauses.append({"segment_type": segment_type})
        if since:
            where_clauses.append({"started_at": {"$gte": since}})
        if until:
            where_clauses.append({"started_at": {"$lte": until}})

        where: dict[str, Any] | None = None
        if len(where_clauses) == 1:
            where = where_clauses[0]
        elif len(where_clauses) > 1:
            where = {"$and": where_clauses}

        try:
            results = self.segments_collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
        except Exception:
            return []

        ids = _first_batch(results.get("ids"))
        documents = _first_batch(results.get("documents"))
        metadatas = _first_batch(results.get("metadatas"))
        distances = _first_batch(results.get("distances"))

        hits: list[dict[str, Any]] = []
        for i, sid in enumerate(ids):
            hits.append(
                {
                    "segment_id": sid,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 1.0,
                }
            )
        return hits

    def segment_count(self) -> int:
        """Return the number of segment embeddings."""
        try:
            return self.segments_collection.count()
        except Exception:
            return 0

    # ─── Episodes collection ─────────────────────────────────────────────

    def add_episode_embedding(
        self,
        episode_id: str,
        text: str,
        metadata: dict[str, Any],
    ) -> None:
        """Upsert one embedding per problem→fix episode."""
        if not text or not text.strip():
            return
        self.episodes_collection.upsert(
            ids=[episode_id],
            documents=[text[:MAX_EMBED_CHARS]],
            metadatas=[metadata],
        )

    def add_episode_embeddings_batch(self, items: list[dict[str, Any]]) -> int:
        """Upsert multiple problem→fix episode embeddings in one ONNX batch.

        Each item: ``{"episode_id": str, "text": str, "metadata": dict}``.
        Items with empty or whitespace-only text are dropped. Chunked at
        CHROMA_BATCH_SIZE for Chroma stability. Returns the number of
        embeddings upserted.
        """
        if not items:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[Any] = []
        for item in items:
            text = item.get("text") or ""
            if not text.strip():
                continue
            ids.append(item["episode_id"])
            documents.append(text[:MAX_EMBED_CHARS])
            metadatas.append(item["metadata"])

        if not ids:
            return 0

        added = 0
        for i in range(0, len(ids), CHROMA_BATCH_SIZE):
            self.episodes_collection.upsert(
                ids=ids[i : i + CHROMA_BATCH_SIZE],
                documents=documents[i : i + CHROMA_BATCH_SIZE],
                metadatas=metadatas[i : i + CHROMA_BATCH_SIZE],
            )
            added += len(ids[i : i + CHROMA_BATCH_SIZE])
        return added

    def search_episodes(
        self,
        query: str,
        n_results: int = 10,
        project_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        has_fix: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic search over problem→fix episode text."""
        where_clauses: list[dict[str, Any]] = []
        if project_id:
            where_clauses.append({"project_id": project_id})
        if has_fix is not None:
            where_clauses.append({"has_fix": has_fix})
        if since:
            where_clauses.append({"ended_at": {"$gte": since}})
        if until:
            where_clauses.append({"ended_at": {"$lte": until}})

        where: dict[str, Any] | None = None
        if len(where_clauses) == 1:
            where = where_clauses[0]
        elif len(where_clauses) > 1:
            where = {"$and": where_clauses}

        try:
            results = self.episodes_collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where,
            )
        except Exception:
            return []

        ids = _first_batch(results.get("ids"))
        documents = _first_batch(results.get("documents"))
        metadatas = _first_batch(results.get("metadatas"))
        distances = _first_batch(results.get("distances"))

        hits: list[dict[str, Any]] = []
        for i, eid in enumerate(ids):
            hits.append(
                {
                    "episode_id": eid,
                    "document": documents[i] if i < len(documents) else "",
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else 1.0,
                }
            )
        return hits

    def episode_count(self) -> int:
        """Return the number of episode embeddings."""
        try:
            return self.episodes_collection.count()
        except Exception:
            return 0

    def delete_session_analysis(self, session_id: str) -> None:
        """Drop a session's episode/segment embeddings ahead of re-analysis.

        Mirrors SQLiteStore.delete_session_analysis — re-extraction mints new
        ids for changed boundaries, so the old vectors must go or they haunt
        semantic search forever. Best-effort: never raises.
        """
        for collection in (self.episodes_collection, self.segments_collection):
            try:
                collection.delete(where={"session_id": session_id})
            except Exception:
                pass

    def reset(self) -> None:
        """Delete and recreate all collections."""
        for name in ("events", "sessions", "projects", "segments", "episodes"):
            try:
                self.client.delete_collection(name=name)
            except Exception:
                pass
        self.events_collection = self.client.get_or_create_collection(name="events")
        self.sessions_collection = self.client.get_or_create_collection(name="sessions")
        self.projects_collection = self.client.get_or_create_collection(name="projects")
        self.segments_collection = self.client.get_or_create_collection(name="segments")
        self.episodes_collection = self.client.get_or_create_collection(name="episodes")
