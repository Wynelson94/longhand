---
confidence: high
id: '0002'
references:
- advisory: false
  lines: null
  path: longhand/parser.py
  pinned_at: '2026-07-11T19:29:10.325279Z'
  sha: c4e50bbda7b0b0a3c6ed239ba30888a54235aaf5102389c4b15d52d6422574f5
  symbol: null
session_id: null
superseded_by: '0006'
supersedes: null
tags:
- parser
- project-inference
- gotcha
- dont-reintroduce
title: Project attribution uses MODE of cwds, not first-event cwd
topic: project-inference-cwd-mode
validations:
- at: '2026-07-10T18:16:41.805585Z'
  by: unknown
- at: '2026-07-10T20:55:48.497843Z'
  by: unknown
written_at: '2026-04-26T05:28:36.808901Z'
written_by: claude-opus-4-7
---

# Project attribution uses MODE of cwds, not first-event cwd

Pre-v0.6 `build_session` attributed a session to the cwd of its first event. Sessions launched from `$HOME` (a common case — `cd ~/Projects/foo && claude`) landed with `project_id = NULL` because the first event's cwd was `$HOME`, not the project. This is the **bsoi-mesh-kit "No session history found despite four real transcripts"** bug — the canonical example that motivated v0.6.

**Current rule:** tally cwds across ALL events in the session, filter out `$HOME` and paths without project markers (no `.git`, no `package.json`, etc.), and pick the mode. If you see code that touches first-event cwd directly when inferring a project, that's the regression. Don't.
