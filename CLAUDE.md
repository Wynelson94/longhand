# Longhand MCP Tools — Efficient Usage Guide

## Quick Decision Tree

When a user asks about past work:

1. **"Do you remember when..."** → Use `recall` FIRST. It handles fuzzy time, project matching, and retrieval in one call, returning a narrative built from conversation segments and session timelines — plus high-precision problem→fix episodes when the work left clean evidence.

2. **"Find X in session Y"** → Use `search` with `session_id` + `context_events` + a natural-language query. Returns matches WITH surrounding conversation. Do NOT paginate `get_session_timeline` manually.

3. **"What happened in session X?"** → Use `get_session_timeline` with `summary_only: true` first to scan, then `search` (with `session_id` + `context_events`) to drill into specifics. Use `tail: N` for how the session ended or the latest events.

4. **"What file did we edit?"** → Use `get_file_history` or `replay_file`.

5. **"What did we commit?"** → Use `find_commits` — pass a `query` for cross-session search, or a `session_id` with no query for one session's chronological git story.

6. **"Where did we leave off on X?"** → Use `recall_project_status` with the project name. Returns recent commits, unresolved issues, last session outcome, and conversation context in one call. Git-aware when git data exists, degrades gracefully without it.

## Anti-Patterns (AVOID)

- **Never paginate `get_session_timeline` in a loop** looking for something. Use `search` with `session_id` + `context_events` instead.
- **Never use `search` without `session_id`** when you know which session to look in. Unscoped search returns noise from all sessions.
- **Never skip `recall`** for "do you remember" questions. It was built for exactly this use case.
- **Don't call the deprecated names** — `search_in_context`, `get_latest_events`, `get_project_timeline`, `get_session_commits`, `get_episode`, `match_project` still answer (with a migration preamble), but the surviving tools take the same parameters directly.

## Tool Pairing Patterns

### Pattern A: Find a discussion in a known session (2-3 calls max)
1. `list_sessions` → identify the session
2. `search(session_id, query, context_events)` → find the discussion with surrounding context
3. (Optional) `get_session_timeline` at a specific offset if you need even more surrounding context

### Pattern B: Recall past work across sessions (1-2 calls)
1. `recall(query)` → get projects, narrative, and any high-precision episodes
2. (Optional) `search(session_id, context_events)` to read the raw conversation around a result

### Pattern C: Pick up a project where you left off (1 call)
1. `recall_project_status(project)` → recent commits, unresolved issues, last outcome, conversation context
2. (Optional) `search(session_id, ...)` to drill into a specific session from the results

### Pattern D: Investigate a file's history (2 calls)
1. `get_file_history(file_path)` → see all edits chronologically
2. `replay_file(session_id, file_path)` → reconstruct exact file state at a point in time

## Key Filters

- `search` accepts: `session_id`, `event_type`, `tool_name`, `file_path_contains`, `project_id`, `project_name` — plus `context_events` (with `session_id`) to wrap each match in its surrounding conversation
- `list_sessions` accepts: `project` (path substring) or `project_id` (+ `since`/`until`) for an outcome-enriched project timeline
- Always use the most specific filter available to reduce noise
- `session_id` supports prefix matching (first 8 chars is usually enough)

## Deeper Tools (less common starting points)

Beyond the decision tree above: `get_session_timeline` with `tail` (the last N events, replaces get_latest_events), `find_episodes` with `episode_id` (full detail: referenced events, diff, post-fix file state), `list_projects` with `match` (fuzzy candidates with scored reasons — "which project did you mean?"), `list_plans` (browse plan-file writes), `get_stats` (store health), and `reconcile` (re-ingest drift) — **pass `fix` explicitly**: `fix=true` heals now; the implicit default flips to dry-run at v1.0.
