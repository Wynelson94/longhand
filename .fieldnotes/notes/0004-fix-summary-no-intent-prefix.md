---
confidence: high
id: '0004'
references:
- lines:
  - 405
  - 458
  path: longhand/analysis/episode_extraction.py
  sha: 5aeb69c605a667efd9e4f06908442492706cd629e097bd226af88a67e5101a30
  symbol: _compose_fix_summary
session_id: null
superseded_by: null
supersedes: null
tags:
- episodes
- embeddings
- migration-v4
- dont-reintroduce
title: Don't prepend labels (`Intent:`) to fix_summary content
topic: fix-summary-no-intent-prefix
written_at: '2026-04-26T05:28:37.019715Z'
written_by: claude-opus-4-7
---

# Don't prepend labels (`Intent:`) to fix_summary content

Pre-v0.8 `_compose_fix_summary` prepended `"Intent: "` to the assistant's intent text "for embedding structure" (per the original code comment). The label leaked into every recall narrative for affected episodes — the audit on the reference corpus found **100 of 204 episodes (49%)** had this. Users saw "Intent: fix the type coercion" in their recall output instead of "fix the type coercion."

v0.8 removed the prefix; **migration v4** (auto-applied on next store open) strips it from existing rows. Embeddings were NOT regenerated — semantic clusters of `"Intent: foo"` and `"foo"` are close enough that ranking is unchanged. The `recall_diff.py` validator confirmed.

**Rule:** don't add structural labels to text that's both stored as content AND embedded for semantic search. The label leaks into user-visible output and delivers nothing the embedding model couldn't infer from the surrounding context. If you find yourself wanting to do this, ask whether the structure should live in a separate column instead.
