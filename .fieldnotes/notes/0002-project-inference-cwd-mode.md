---
confidence: high
id: '0002'
references:
- lines: null
  path: longhand/parser.py
  sha: d577dd844bffe6eed691048e6cb744f87ffe4bab527686d5fc50319991fef1b3
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
written_at: '2026-04-26T05:28:36.808901Z'
written_by: claude-opus-4-7
---

# Project attribution uses MODE of cwds, not first-event cwd

Pre-v0.6 `build_session` attributed a session to the cwd of its first event. Sessions launched from `$HOME` (a common case — `cd ~/Projects/foo && claude`) landed with `project_id = NULL` because the first event's cwd was `$HOME`, not the project. This is the **bsoi-mesh-kit "No session history found despite four real transcripts"** bug — the canonical example that motivated v0.6.

**Current rule:** tally cwds across ALL events in the session, filter out `$HOME` and paths without project markers (no `.git`, no `package.json`, etc.), and pick the mode. If you see code that touches first-event cwd directly when inferring a project, that's the regression. Don't.
