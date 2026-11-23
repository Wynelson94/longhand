"""Every Chroma collection Longhand creates uses the shared CPU embedding model."""

from __future__ import annotations

from pathlib import Path

import pytest

from longhand.storage import vector_store as vs

COLLECTIONS = ("events", "sessions", "projects", "segments", "episodes")


class _FakeEmbedding:
    """Stands in for the ONNX model so the test needs no model download."""

    def __init__(self, preferred_providers=None):
        self.preferred_providers = preferred_providers

    def __call__(self, input):
        return [[0.0, 1.0] for _ in input]

    @staticmethod
    def name() -> str:
        return "longhand-test-fake"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return _FakeEmbedding()

    def is_legacy(self) -> bool:
        return False


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vs, "ONNXMiniLM_L6_V2", _FakeEmbedding)
    return vs.VectorStore(tmp_path / "chroma")


def _embedding_names(store) -> dict[str, str]:
    return {
        name: getattr(store, f"{name}_collection")._embedding_function.name()
        for name in COLLECTIONS
    }


def test_collections_use_the_shared_embedding_after_init(store):
    assert set(_embedding_names(store).values()) == {"longhand-test-fake"}


def test_reset_keeps_the_shared_embedding(store):
    store.reset()

    assert set(_embedding_names(store).values()) == {"longhand-test-fake"}


def test_recreated_collection_keeps_the_shared_embedding(store):
    store.client.delete_collection(name="episodes")
    store.episodes_collection = store._recreate_collection("episodes")

    assert store.episodes_collection._embedding_function.name() == "longhand-test-fake"
