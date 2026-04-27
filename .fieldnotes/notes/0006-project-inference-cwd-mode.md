---
confidence: high
id: '0006'
references:
- lines:
  - 501
  - 563
  path: longhand/parser.py
  sha: a008ae34c0665ddbf558fc103344028e53430f9ceb8f8b14263dd7c8d1621e23
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
