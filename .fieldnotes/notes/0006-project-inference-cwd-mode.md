---
confidence: high
id: '0006'
references:
- advisory: false
  lines:
  - 617
  - 683
  path: longhand/parser.py
  pinned_at: '2026-06-11T17:58:33.908597Z'
  sha: b8b583658f110c8b62314e57d7b127ca88b40e9631bd32f96644a589055f18e2
  symbol: JSONLParser.build_session
session_id: null
superseded_by: null
supersedes: '0002'
tags:
- parser
- project-inference
- gotcha
- dont-reintroduce
title: Project attribution uses MODE of cwds, not first-event cwd
topic: project-inference-cwd-mode
written_at: '2026-04-26T05:29:06.166137Z'
written_by: claude-opus-4-7
---

# Project attribution uses MODE of cwds, not first-event cwd

Pre-v0.6 `JSONLParser.build_session` attributed a session to the cwd of its first event. Sessions launched from `$HOME` (a common case — `cd ~/Projects/foo && claude`) landed with `project_id = NULL` because the first event's cwd was `$HOME`, not the project. This is the **bsoi-mesh-kit "No session history found despite four real transcripts"** bug — the canonical example that motivated v0.6.

**Current rule:** tally cwds across ALL events in the session, filter out `$HOME` and paths without project markers (no `.git`, no `package.json`, etc.), and pick the mode. If you see code that touches first-event cwd directly when inferring a project, that's the regression. Don't.
