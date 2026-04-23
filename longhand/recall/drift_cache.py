"""
Filesystem cache for the drift-detection scan.

`_detect_project_drift` in `recall_pipeline.py` needs to know, for each
on-disk JSONL, which project canonical paths it references. Computing that
fresh on every `recall_project_status` call means opening and parsing every
JSONL on disk — 2+ seconds on a modest corpus. This module caches the
result keyed by `(transcript_path, mtime)`; stale entries are invalidated
automatically when mtime changes.

The cache file lives at `~/.longhand/cache/jsonl_project_map.json` and is
written atomically (tempfile + rename).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from longhand.analysis.project_inference import find_project_root_strict

_CACHE_VERSION = 1
_DRIFT_SCAN_LINE_LIMIT = 20_000


@dataclass
class DriftCacheEntry:
    """All the project-path signals extracted from one JSONL."""

    mtime: float
    raw_cwds: set[str] = field(default_factory=set)
    resolved_roots: set[str] = field(default_factory=set)

    def references(self, canonical_path: str, target_resolved: Path | None) -> bool:
        """True if this JSONL referenced the given canonical path."""
        if canonical_path in self.raw_cwds:
            return True
        if target_resolved is not None and str(target_resolved) in self.resolved_roots:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mtime": self.mtime,
            "raw_cwds": sorted(self.raw_cwds),
            "resolved_roots": sorted(self.resolved_roots),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriftCacheEntry:
        return cls(
            mtime=float(data.get("mtime", 0.0)),
            raw_cwds=set(data.get("raw_cwds", [])),
            resolved_roots=set(data.get("resolved_roots", [])),
        )


class DriftCache:
    """Load/save the `(transcript_path, mtime) → DriftCacheEntry` map.

    Callers ask for `get_or_compute(path)` and don't need to think about
    invalidation — mtime change on the file triggers a recompute, and a
    missing cache file rebuilds from scratch.
    """

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._entries: dict[str, DriftCacheEntry] = {}
        self._dirty = False
        self._load()

    # ─── public API ──────────────────────────────────────────────────────

    def get_or_compute(self, jsonl_path: Path) -> DriftCacheEntry | None:
        """Return the entry for this JSONL, computing on cache miss/stale.

        Returns None if the file can't be stat'd or read — callers should
        skip such files.
        """
        try:
            mtime = jsonl_path.stat().st_mtime
        except (OSError, PermissionError):
            return None

        key = str(jsonl_path)
        cached = self._entries.get(key)
        if cached is not None and cached.mtime == mtime:
            return cached

        computed = _scan_jsonl(jsonl_path, mtime)
        if computed is not None:
            self._entries[key] = computed
            self._dirty = True
        return computed

    def prune(self, valid_paths: set[str]) -> None:
        """Drop entries whose transcript_path is no longer in `valid_paths`."""
        stale_keys = [k for k in self._entries if k not in valid_paths]
        for k in stale_keys:
            del self._entries[k]
        if stale_keys:
            self._dirty = True

    def save(self) -> None:
        """Atomic write: tempfile + rename. No-op if nothing changed."""
        if not self._dirty:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _CACHE_VERSION,
            "entries": {k: v.to_dict() for k, v in self._entries.items()},
        }
        # Write to a temp file in the same dir so rename is atomic on POSIX.
        fd, tmp_name = tempfile.mkstemp(
            prefix=".jsonl_project_map-", suffix=".json.tmp",
            dir=str(self.cache_path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp_name, self.cache_path)
        except Exception:
            # Best-effort cleanup if the rename didn't happen.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._dirty = False

    # ─── internals ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted cache — rebuild from scratch on first write.
            self._entries = {}
            self._dirty = True
            return
        if data.get("version") != _CACHE_VERSION:
            # Version mismatch — rebuild.
            self._entries = {}
            self._dirty = True
            return
        entries = data.get("entries", {})
        for k, v in entries.items():
            try:
                self._entries[k] = DriftCacheEntry.from_dict(v)
            except Exception:
                continue


def _scan_jsonl(jsonl_path: Path, mtime: float) -> DriftCacheEntry | None:
    """Read a JSONL once, return a DriftCacheEntry with every cwd signal found.

    Bounded by `_DRIFT_SCAN_LINE_LIMIT` so a pathological file can't block the
    pipeline. Multi-project sessions often visit their real project hundreds
    of events in, so we scan the whole file (up to the limit) with no early
    exit — the cost per file is amortized by the cache.
    """
    entry = DriftCacheEntry(mtime=mtime)
    seen_cwds: set[str] = set()
    try:
        with jsonl_path.open() as f:
            for i, line in enumerate(f):
                if i >= _DRIFT_SCAN_LINE_LIMIT:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd")
                if not cwd or cwd in seen_cwds:
                    continue
                seen_cwds.add(cwd)
                entry.raw_cwds.add(cwd)

                # Resolve + walk up to project root
                try:
                    p = Path(cwd).resolve()
                except (OSError, PermissionError):
                    continue
                if p.is_file():
                    p = p.parent
                # Always record the resolved form so /tmp → /private/tmp
                # lookups can match even when no marker was found.
                entry.resolved_roots.add(str(p))
                root = find_project_root_strict(p)
                if root is not None:
                    entry.resolved_roots.add(str(root))
    except (OSError, PermissionError):
        return None
    return entry


def default_cache_path(data_dir: Path | None = None) -> Path:
    """The standard cache location: `<data_dir>/cache/jsonl_project_map.json`.

    Defaults to `~/.longhand/cache/jsonl_project_map.json` when no data_dir
    is passed, matching the MCP server / CLI default.
    """
    if data_dir is None:
        data_dir = Path.home() / ".longhand"
    return data_dir / "cache" / "jsonl_project_map.json"
