"""
ChromaDB-backed vector storage for semantic search over event content.

Events are embedded for semantic retrieval, but the authoritative
record lives in SQLite — the vector store only holds what's needed
for search (event_id, truncated content, filter metadata).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

from longhand.types import Event, EventType


# Limit embedded text length to keep Chroma performant.
# The full content is always retrievable from SQLite by event_id.
MAX_EMBED_CHARS = 2000


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

    def add_events(self, events: list[Event]) -> int:
        """Add a batch of events to the vector index.

        Duplicate event_ids are overwritten (upsert).
        """
        if not events:
            return 0

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []

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

        # Chunk into batches of 500 for Chroma stability
        added = 0
        for i in range(0, len(ids), 500):
            batch_ids = ids[i : i + 500]
            batch_docs = documents[i : i + 500]
            batch_meta = metadatas[i : i + 500]
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

        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]

        hits: list[dict[str, Any]] = []
        for i, event_id in enumerate(ids):
            hits.append({
                "event_id": event_id,
                "document": documents[i] if i < len(documents) else "",
                "metadata": metadatas[i] if i < len(metadatas) else {},
                "distance": distances[i] if i < len(distances) else 1.0,
            })

        # Apply file_path_contains as post-filter (Chroma doesn't do LIKE)
        if file_path_contains:
            hits = [
                h for h in hits
                if file_path_contains.lower() in (h["metadata"].get("file_path") or "").lower()
            ]

        return hits

    def count(self) -> int:
        return self.events_collection.count()

    def reset(self) -> None:
        """Delete and recreate the events collection."""
        try:
            self.client.delete_collection(name="events")
        except Exception:
            pass
        self.events_collection = self.client.get_or_create_collection(name="events")
