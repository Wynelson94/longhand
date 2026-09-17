---
confidence: high
id: '0001'
references:
- lines:
  - 708
  - 731
  path: longhand/recall/recall_pipeline.py
  sha: 84b7d622005131e0387a9e1a6f92a34e6131299f5e584382a47d5a5a6b795c82
  symbol: staleness_banner
session_id: null
superseded_by: null
supersedes: null
tags:
- staleness
- mcp
- recall
- gotcha
- recurring-class
title: Any user-facing query tool MUST surface staleness
topic: silent-failure-staleness-class
written_at: '2026-04-26T05:28:36.702182Z'
written_by: claude-opus-4-7
---

# Any user-facing query tool MUST surface staleness

Three iterations (v0.6 → v0.7 → v0.8.1) proved this is a recurring bug class: when a project's transcripts exist on disk but haven't been ingested yet, any query tool scoped to that project returns clean-looking empty results. Users (and Claude sessions) read those as "nothing happened" instead of "the index is stale."

- **v0.6**: `recall_project_status` got drift detection (`session_count_indexed`, `session_count_on_disk`, `last_ingested_at`, `stale_reason`) and a ⚠ banner.
- **v0.7**: `search` learned to auto-scope to a matched project at confidence ≥0.8.
- **v0.8.1**: same staleness signal had to spread to `search` and `list_sessions` after a live miss on `search("portneuf junk removal")` — `hits: []` despite real transcripts on disk.

**Rule:** any new user-facing tool that scopes to a project must wrap `staleness_banner()` (the cache-backed helper introduced in v0.8.1) and prepend its banner to the response. Don't return clean-looking empty results for stale projects — fail loud.

`staleness_banner` is the right pin (not `_detect_project_drift`) because the banner is what callers should be surfacing; the drift detector is the underlying primitive.
