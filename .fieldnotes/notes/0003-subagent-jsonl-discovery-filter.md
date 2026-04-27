---
confidence: high
id: '0003'
references:
- lines:
  - 566
  - 599
  path: longhand/parser.py
  sha: ed8f380e42045a3dfaaad3b9b3f2bb53588efe46a930b67d8cc6fb1a8a4db7dd
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
written_at: '2026-04-26T05:28:36.914677Z'
written_by: claude-opus-4-7
---

# Subagent JSONLs at `*/subagents/*.jsonl` are NOT independent sessions

Subagent transcripts live at `<projects-dir>/.../<session-id>/subagents/<id>.jsonl`. They're referenced from the parent session's events, not standalone sessions. Pre-v0.6 ingest treated them as top-level transcripts, so reconcile double-counted totals (the bug surfaced when `longhand reconcile` was added and started reporting wildly inflated session counts).

`discover_sessions` filters three classes:

1. `/subagents/` in the path — subagent transcripts
2. `pytest-of-` in the path — pytest tmpdir leftovers
3. `skill-injections` or `vercel-plugin` in the filename — internal plugin files

**If you write a new transcript discovery path** (custom ingest source, a new recall scope, etc.), replicate this filter or you'll re-introduce the double-count. Better: call `discover_sessions` directly when you can.
