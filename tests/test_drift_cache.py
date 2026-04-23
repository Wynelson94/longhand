"""Tests for the drift-detection cache."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from longhand.recall.drift_cache import DriftCache, DriftCacheEntry, _scan_jsonl


def _write_jsonl(path: Path, cwds: list[str]) -> None:
    """Write a minimal JSONL where each line carries one of the given cwds."""
    lines = []
    for i, cwd in enumerate(cwds):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"u{i}",
                    "sessionId": "s",
                    "timestamp": "2026-04-23T00:00:00Z",
                    "cwd": cwd,
                    "message": {"role": "user", "content": "hi"},
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


def test_cold_miss_populates_cache(tmp_path):
    """First lookup scans the JSONL and stores the entry."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, ["/tmp/foo", "/tmp/foo/src", "/tmp/foo"])

    cache = DriftCache(tmp_path / "cache.json")
    entry = cache.get_or_compute(jsonl)
    assert entry is not None
    # Deduped raw cwds
    assert "/tmp/foo" in entry.raw_cwds
    assert "/tmp/foo/src" in entry.raw_cwds

    cache.save()
    assert (tmp_path / "cache.json").exists()


def test_warm_hit_skips_scan(tmp_path, monkeypatch):
    """A second lookup with unchanged mtime must NOT re-read the JSONL."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, ["/tmp/foo"])

    cache_path = tmp_path / "cache.json"
    cache = DriftCache(cache_path)
    cache.get_or_compute(jsonl)
    cache.save()

    # Fresh cache instance — loads from disk.
    cache2 = DriftCache(cache_path)

    call_count = {"n": 0}

    def fake_scan(*args, **kwargs):
        call_count["n"] += 1
        return _scan_jsonl(*args, **kwargs)

    monkeypatch.setattr(
        "longhand.recall.drift_cache._scan_jsonl", fake_scan
    )
    entry = cache2.get_or_compute(jsonl)
    assert entry is not None
    assert call_count["n"] == 0, "warm cache must not re-scan the JSONL"


def test_mtime_change_invalidates_entry(tmp_path):
    """Touching the JSONL with a new mtime invalidates its cache entry."""
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, ["/tmp/foo"])

    cache = DriftCache(tmp_path / "cache.json")
    cache.get_or_compute(jsonl)

    # Overwrite content and bump mtime.
    time.sleep(0.01)
    _write_jsonl(jsonl, ["/tmp/bar"])
    os.utime(jsonl, (time.time() + 1, time.time() + 1))

    entry = cache.get_or_compute(jsonl)
    assert entry is not None
    # New entry reflects new cwd, not the cached old one.
    assert "/tmp/bar" in entry.raw_cwds
    assert "/tmp/foo" not in entry.raw_cwds


def test_corrupted_cache_recovers(tmp_path):
    """Garbage on disk at the cache path should not crash the loader."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text("{ this is not valid json")

    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(jsonl, ["/tmp/foo"])

    cache = DriftCache(cache_path)
    entry = cache.get_or_compute(jsonl)
    assert entry is not None
    # After save, the cache file should be valid JSON again.
    cache.save()
    json.loads(cache_path.read_text())  # no exception


def test_prune_drops_missing_paths(tmp_path):
    """Entries whose transcript_path isn't in the valid set are dropped."""
    cache = DriftCache(tmp_path / "cache.json")
    cache._entries["/no/longer/exists.jsonl"] = DriftCacheEntry(mtime=1.0)
    cache._entries["/still/here.jsonl"] = DriftCacheEntry(mtime=2.0)

    cache.prune({"/still/here.jsonl"})
    assert "/no/longer/exists.jsonl" not in cache._entries
    assert "/still/here.jsonl" in cache._entries


def test_references_matches_resolved_paths(tmp_path, monkeypatch):
    """An entry references a project when the resolved path was captured."""
    jsonl = tmp_path / "session.jsonl"
    # Use a real path that exists and is a project root.
    project = tmp_path / "real-project"
    project.mkdir()
    (project / ".git").mkdir()
    _write_jsonl(jsonl, [str(project)])

    cache = DriftCache(tmp_path / "cache.json")
    entry = cache.get_or_compute(jsonl)
    assert entry is not None
    target = Path(str(project)).resolve()
    assert entry.references(str(project), target)
    # Different project that the JSONL never touched: no match.
    other = tmp_path / "other"
    other.mkdir()
    assert not entry.references(str(other), other.resolve())


def test_save_is_atomic(tmp_path):
    """Save must not leave a stale .tmp file around on success."""
    cache = DriftCache(tmp_path / "cache.json")
    cache._entries["/x.jsonl"] = DriftCacheEntry(mtime=1.0)
    cache._dirty = True
    cache.save()

    leftover_tmps = [p for p in tmp_path.iterdir() if p.name.startswith(".jsonl_project_map-")]
    assert leftover_tmps == []
