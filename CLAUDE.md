# Longhand MCP Tools — Efficient Usage Guide

## Quick Decision Tree

When a user asks about past work:

1. **"Do you remember when..."** → Use `recall` FIRST. It handles fuzzy time, project matching, and retrieval in one call, returning a narrative built from conversation segments and session timelines — plus high-precision problem→fix episodes when the work left clean evidence. If the narrative leads with "Older than the other matches," the top hit is 30+ days old and 30+ days older than the runner-up — re-query with a time phrase (e.g. "this week") if you wanted current work; ranking itself hasn't changed.

2. **"Find X in session Y"** → Use `search` with `session_id` + `context_events` + a natural-language query. Returns matches WITH surrounding conversation. Do NOT paginate `get_session_timeline` manually.

3. **"What happened in session X?"** → Use `get_session_timeline` with `summary_only: true` first to scan, then `search` (with `session_id` + `context_events`) to drill into specifics. Use `tail: N` for how the session ended or the latest events.

4. **"What file did we edit?"** → Use `get_file_history` or `replay_file`.

5. **"What did we commit?"** → Use `find_commits` — pass a `query` for cross-session search, or a `session_id` with no query for one session's chronological git story.

6. **"Where did we leave off on X?"** → Use `recall_project_status` with the project name. Returns recent commits, unresolved issues, last session outcome, and conversation context in one call. Git-aware when git data exists, degrades gracefully without it.

## Anti-Patterns (AVOID)

- **Never paginate `get_session_timeline` in a loop** looking for something. Use `search` with `session_id` + `context_events` instead.
- **Never use `search` without `session_id`** when you know which session to look in. Unscoped search returns noise from all sessions.
- **Never skip `recall`** for "do you remember" questions. It was built for exactly this use case.
- **Don't call the retired names** — `search_in_context`, `get_latest_events`, `get_project_timeline`, `get_session_commits`, `get_episode`, `match_project` left the tool listing at 1.0. They still answer forever (with a migration preamble) so older docs never hard-fail. Most surviving tools take the same parameters directly (`search_in_context(session_id, context_events)` → `search(session_id, context_events)`); two were renamed: `get_latest_events(limit)` → `get_session_timeline(tail)`, and `match_project(query, top_k)` → `list_projects(match, limit)`.
- **The in-progress session usually isn't visible to `recall` or `search`.** Both are vector-backed, and the Stop hook's live tail never embeds and never sets `project_id`. Embeddings normally land at `SessionEnd` — or sooner if a `reconcile(fix=true)` pass runs first, since a live-tailed session's missing `project_id` puts it in reconcile's re-ingest bucket. For "what are we doing right now" questions about the CURRENT session, use `get_session_timeline` (works immediately via the Stop hook's live tail) — or, for a Codex thread that's still active, the `longhand-shared` server's keyword `search`.

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

- `search` accepts: `session_id`, `event_type`, `tool_name`, `file_path_contains`, `project_id`, `project_name` — plus `context_events` (with `session_id`) to wrap each match in its surrounding conversation. In context mode, only `session_id` and `event_type` apply — `tool_name`/`file_path_contains`/project filters are ignored.
- `list_sessions` accepts: `project` (path substring) or `project_id` (+ `since`/`until`) for an outcome-enriched project timeline
- Always use the most specific filter available to reduce noise
- `session_id` supports prefix matching (first 8 chars is usually enough) on this server — but the `longhand-shared` server's tools need the exact `session_id` (no prefix matching there)
- **`search` auto-scopes to a project** when the query text names one and you didn't pass `session_id`/`project_id`/`project_name` yourself: the payload becomes `{auto_scoped_to, auto_scope_hint, hits}` instead of a plain array. `list_sessions` and `search` can also come back as `{stale: true, stale_reason, ...}` when on-disk transcripts outrun the index — call `reconcile` with `fix=true` to catch up, then retry.

## Deeper Tools (less common starting points)

Beyond the decision tree above: `get_session_timeline` with `tail` (the last N events, replaces get_latest_events), `find_episodes` with `episode_id` (full detail: referenced events, diff, post-fix file state), `list_projects` with `match` (fuzzy candidates with scored reasons — "which project did you mean?"), `list_plans` (browse plan-file writes), `get_stats` (store health), and `reconcile` (re-ingest drift) — **`reconcile` defaults to a dry run; pass `fix=true` to actually heal.**

Output size: `search`, `get_session_timeline`, `recall`, `recall_project_status`, and `find_commits` accept `max_chars` and truncate with a pagination hint. `get_file_history` and `replay_file` do not, but not in the same way: `get_file_history` cuts each entry's `old_content`/`new_content` to 800 characters, so what's unbounded is the *number* of edit rows — scope it to a `session_id` when a file has a long history. `replay_file` returns the complete file as of the point you asked for; `at_event_id` only picks *which* point in time, not how much of the file comes back, so it doesn't help with a large file.

## Codex sessions (1.1.0+)

Codex Desktop / CLI threads live in the same archive with `codex:<thread-id>` session ids — every one starts with the same 6-character `codex:` literal, so the usual "first 8 characters is enough" prefix trick rarely disambiguates; use `list_sessions` (or `longhand-shared`'s `list_sessions(source="codex")`) to find the exact id first. This server lists and pages them (`list_sessions`, `get_session_timeline`) and `find_commits` sees commits made from Codex — but `recall` and semantic `search` only see a Codex session once it has been finalized: 30 minutes after the thread goes quiet, *provided something is polling on that schedule* (the reconciler or a `codex-sync --watch`/launchd loop — it doesn't happen on its own otherwise). `longhand codex-sync --semantic` finalizes it immediately. For "what did I do in Codex" questions about a thread that is still active, use the `longhand-shared` server's keyword `search` — literal phrases only, exact `session_id` required (no prefix matching on that server), 2,000-character excerpts per hit with `limit` capped at 50; page a longer record with `get_event_text`.
