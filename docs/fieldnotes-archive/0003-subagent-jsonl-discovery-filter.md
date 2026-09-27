---
confidence: high
id: '0003'
references:
- advisory: false
  lines:
  - 686
  - 719
  path: longhand/parser.py
  pinned_at: '2026-07-10T18:16:27.060011Z'
  sha: 16684994325660f790ada066ff5263e4254277abe9bf3ac507896b9299a1cda1
  symbol: discover_sessions
session_id: null
superseded_by: null
supersedes: null
tags:
- parser
- ingest
- reconcile
- dont-reintroduce
title: Subagent JSONLs at */subagents/*.jsonl are NOT independent sessions
topic: subagent-jsonl-discovery-filter
validations:
- at: '2026-07-10T18:16:41.913822Z'
  by: unknown
written_at: '2026-04-26T05:28:36.914677Z'
written_by: claude-opus-4-7
---

# Subagent JSONLs at `*/subagents/*.jsonl` are NOT independent sessions

Subagent transcripts live at `<projects-dir>/.../<session-id>/subagents/<id>.jsonl`. They're referenced from the parent session's events, not standalone sessions. Pre-v0.6 ingest treated them as top-level transcripts, so reconcile double-counted totals (the bug surfaced when `longhand reconcile` was added and started reporting wildly inflated session counts).

`discover_sessions` filters three classes:

1. `_is_subagent_transcript(path)` — checks `"subagents" in path.parts` (path *components*, not a substring: `"/subagents/" in str(path)` never matched on Windows backslash paths, so subagent transcripts polluted the corpus there; fixed in v0.11.2)
2. `pytest-of-` in the path — pytest tmpdir leftovers
3. `skill-injections` or `vercel-plugin` in the filename — internal plugin files

**If you write a new transcript discovery path** (custom ingest source, a new recall scope, etc.), call `_is_subagent_transcript` / replicate this filter or you'll re-introduce the double-count. Better: call `discover_sessions` directly when you can.
