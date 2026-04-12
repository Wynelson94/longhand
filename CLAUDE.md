# Longhand MCP Tools — Efficient Usage Guide

## Quick Decision Tree

When a user asks about past work:

1. **"Do you remember when..."** → Use `recall` FIRST. It handles fuzzy time, project matching, and episode retrieval in one call.

2. **"Find X in session Y"** → Use `search_in_context` with the session_id + natural language query. Returns matches WITH surrounding conversation. Do NOT paginate `get_session_timeline` manually.

3. **"What happened in session X?"** → Use `get_session_timeline` with `summary_only: true` first to scan, then `search_in_context` to drill into specifics.

4. **"What file did we edit?"** → Use `get_file_history` or `replay_file`.

5. **"What did we commit?"** → Use `find_commits` (cross-session) or `get_session_commits` (single session).

6. **"Where did we leave off on X?"** → Use `recall_project_status` with the project name. Returns recent commits, unresolved issues, last session outcome, and conversation context in one call. Git-aware when git data exists, degrades gracefully without it.

## Anti-Patterns (AVOID)

- **Never paginate `get_session_timeline` in a loop** looking for something. Use `search_in_context` or `search` with `session_id` filter instead.
- **Never use `search` without `session_id`** when you know which session to look in. Unscoped search returns noise from all sessions.
- **Never skip `recall`** for "do you remember" questions. It was built for exactly this use case.

## Tool Pairing Patterns

### Pattern A: Find a discussion in a known session (2-3 calls max)
1. `list_sessions` → identify the session
2. `search_in_context(session_id, query)` → find the discussion with surrounding context
3. (Optional) `get_session_timeline` at specific offset if you need more surrounding context

### Pattern B: Recall past work across sessions (1-2 calls)
1. `recall(query)` → get projects, episodes, narrative
2. (Optional) `search_in_context` to read the raw conversation around an episode

### Pattern C: Pick up a project where you left off (1 call)
1. `recall_project_status(project)` → get recent commits, unresolved issues, last outcome, conversation context
2. (Optional) `search_in_context` to drill into a specific session from the results

### Pattern D: Investigate a file's history (2 calls)
1. `get_file_history(file_path)` → see all edits chronologically
2. `replay_file(session_id, file_path)` → reconstruct exact file state at a point in time

## Key Filters

- `search` and `search_in_context` accept: `session_id`, `event_type`, `tool_name`, `file_path_contains`, `project_name`
- Always use the most specific filter available to reduce noise
- `session_id` supports prefix matching (first 8 chars is usually enough)
