---
confidence: high
id: '0005'
references:
- lines:
  - 24
  - 42
  path: longhand/analysis/episode_extraction.py
  sha: bcdf94d09cf2c69832bc36e10935ba671af4c2718e47fc38a609e2f0cae742d7
  symbol: _truncate_at_boundary
session_id: null
superseded_by: null
supersedes: null
tags:
- episodes
- truncation
- code-quality
- dont-reintroduce
title: Truncate at clean boundaries, not byte slices
topic: truncate-at-clean-boundary
written_at: '2026-04-26T05:28:37.125833Z'
written_by: claude-opus-4-7
---

# Truncate at clean boundaries, not byte slices

Pre-v0.8 diff content truncation used a hard `[:120]` byte slice and landed mid-token: `phoneNum'`, `family?:'`, `strin'`. Visible in every recall narrative containing a truncated diff.

`_truncate_at_boundary(text, budget)` exists to do this right: walk back from `budget` to the last whitespace and append `…`. Use it for any truncation of code/text destined for storage or display. **Don't reach for `text[:N]`.**

Forward-only fix: existing rows stay until the next reingestion. The shape of this bug-class — "we truncated bytes when we should have truncated tokens" — should make you suspicious of any other `[:N]` patterns you see in episode/narrative paths.
