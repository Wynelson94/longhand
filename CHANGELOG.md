# Changelog

All notable changes to Longhand are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

For pre-v0.6 releases, the canonical record is the annotated git tag —
`git show v0.5.13` etc. Entries below are reverse-engineered from the
commits and tag annotations of those releases.

---

## [0.11.2] — 2026-07-10

Bugfix release from a full audit of the codebase and a live 324-session corpus:
multi-process reliability, Windows correctness, and a privacy gap in `redact`.
No schema changes and no new migrations — deliberately, so the migration-race
fix below reaches every install before any future migration ships.

### Fixed

- **Background semantic-index catch-up actually runs again.** The recall
  fallback spawned `python -m longhand.cli` — a package with no `__main__.py` —
  so every background ingest died instantly, and silently (output went to a log
  file, exceptions were swallowed). The spawn now targets `python -m longhand`,
  and a regression test asserts the spawn target is importable and runnable so
  this class of bug can't ship again. If your ChromaDB vector index has lagged
  behind the main database, it will converge over subsequent recalls. (#41)
- **Parallel sessions no longer race ChromaDB.** SessionEnd ingest was the one
  write path that never took the ingest lock, so two sessions ending together
  could open concurrent ChromaDB writers on the same directory (lock errors,
  HNSW-index corruption risk). It now claims the lock and — if a bulk ingest
  holds it — defers with a success exit instead of failing; `reconcile` or the
  background sweep picks the session up later. (#41)
- **The Stop hook can no longer crash on store construction.** `ingest-live`
  promised "never raises" but built the store, checked the tail cursor, and
  claimed the lock *outside* its guard — a corrupted Chroma dir, a full disk,
  or a migration race would have crashed the hook every turn. The whole body is
  now guarded; init failures return a summary dict and exit 0. (#41)
- **Concurrent first-run migrations no longer crash the loser.** Two processes
  applying the same migration right after an upgrade (parallel session hooks,
  the CLI, and the MCP server all construct stores) raced `schema_version` and
  the loser crashed with `sqlite3.IntegrityError`; it now waits for the
  winner's version row and continues. SQLite `busy_timeout` also raised
  5s → 30s for compaction-heavy parallel writes. (#41)
- **`find_episodes` no longer returns `[]` by default on noisy corpora.** The
  `has_fix=true` default filtered in Python *after* SQL's
  `ORDER BY ended_at DESC LIMIT`, so when the newest N episodes were fixless
  extraction noise, every with-fix episode fell past the limit and the tool's
  default call came back empty. The filter now lives in the SQL `WHERE`. (#44)
- **Windows: subagent transcripts are no longer ingested as real sessions.**
  The `"/subagents/"` substring check never matched backslash paths; the
  filter now checks path components. (#44)
- **Windows: drift scan no longer crashes on non-ASCII transcripts.** A bare
  `open()` used the platform encoding (cp1252 on Windows) with strict
  decoding, and the resulting `UnicodeDecodeError` escaped through
  `recall_project_status`. Now UTF-8 with `errors=replace`, matching the main
  parser. (#44)
- **Outcome classifier: error → clean → error is "stuck", not "fixed".** A
  clean result now only counts as "ended clean" when it came after the *last*
  error, so sessions that ended broken stop inflating the fixed count.
  Historical labels are unchanged unless you re-run `longhand analyze --all`.
  (#44)
- **`redact --apply` now actually covers segments — and git commit messages.**
  The retroactive-redaction table map named a nonexistent `segments` table
  (the real one is `conversation_segments`), and per-table error handling
  silently skipped it — so segment topics (the verbatim first user message),
  summaries, and keywords were never scanned. Git commit messages weren't
  listed at all. Both are covered now; re-run `longhand redact --apply` if
  you've relied on it. (#44)

## [0.11.1] — 2026-06-18

Bugfix release: per-project rollup counters were inflated, and sessions could be
filed under the wrong project. No schema-breaking changes; a one-time migration
repairs existing databases automatically on first open.

### Fixed

- **Project misattribution: sessions no longer drift to the wrong project.**
  `session.cwd` (set by the Stop-hook live tail via `MAX(cwd)`, a meaningless
  lexicographic pick) and `project_id` (set only by the full analysis pass) were
  written by independent paths and could desync — so work launched from `$HOME`
  ended up filed under the home catch-all project, invisible to per-project
  recall. On a real 265-session corpus, 35 sessions (13%) were affected. Now
  project attribution and cwd are always derived together from the same events
  (`attribute_session_project()`), and the live tail derives cwd the same way
  `build_session` does instead of `MAX(cwd)`. New `longhand reattribute` command
  re-derives every session's project from the **events table** (not the
  transcript, which may have rotated away) to repair existing databases.
- **`projects.session_count` and `projects.total_edits` no longer double-count.**
  `upsert_project()` incremented both columns on *every* (re-)ingest of a session
  — SessionEnd, the live-tail Stop hook's analysis path, and `reconcile`
  re-ingests — so they counted ingest events, not distinct sessions. On a real
  corpus the home directory showed 2,068 "sessions" against 264 real ones (and
  53,952 edits against ~7,200 actual). The columns are now derived rollups,
  recomputed authoritatively from the `sessions` table
  (`recompute_project_stats()`) after each session is attached. Affected
  `longhand projects`, the `list_projects` MCP tool, and `recall_project_status`;
  raw session/event data, search, and recall were never affected.
- **Migration v6 backfills existing databases**, recomputing both columns for all
  projects from the `sessions` table on first open. Idempotent and non-destructive.

## [0.11.0] — 2026-06-09

UX release: the CLI gets organized, the stats get honest, and first-run gets smarter. No breaking changes — every existing command and flag still works.

### Changed

- **`longhand --help` is now grouped.** Commands render in six panels — Recall, Archaeology, Browse & insights, Data, Setup & health, Plumbing — instead of a 40-command alphabetical list. (#28)
- **Plumbing commands are hidden** (still fully callable, exact names unchanged): `ingest-session`, `ingest-live`, `context`, `backfill-episodes`, `mcp-server`. These are hook/desktop-config entry points, not user commands. (#28)
- **`analyze --all` now also rebuilds episode embeddings**, absorbing what `reanalyze` did. `reanalyze` remains as a hidden deprecated alias that warns and delegates — it will be removed in v1.0. (#28)
- **Honest episode stats.** `longhand stats` and the `get_stats` MCP tool now split out `low_confidence_episodes` (fixless extractions below 0.5 confidence — probes and tool churn, not real problems) and report `resolved_rate_pct` over substantive episodes only. Your resolved rate was never as bad as the old denominator made it look. (#28)
- **First-run output teaches with your own data.** `setup` ends with an indexed-summary line (sessions · projects · episodes) and suggests `longhand status "<your top project>"` instead of a placeholder. (#28)

### Deprecated

- `longhand reanalyze` → use `longhand analyze --all`. Alias removed in v1.0.

## [0.10.0] — 2026-06-09

Feature release: opt-in secret redaction, plus recall-quality and toolchain hardening.

### Added

- **Secret redaction at ingest (opt-in).** `longhand config --set redact.enabled=true` masks secret-shaped strings — Anthropic/OpenAI/AWS/GitHub/Google/Slack/Stripe keys, SSH key headers, JWTs, database URLs with passwords, SSNs, and Luhn-validated card numbers — at parse time, before they reach SQLite, the raw JSON blobs, or the vector index. Masks keep only the first/last 4 characters and the length (`sk-a…wxyz (len=50)`). Default is off: Longhand stays forensic unless you opt in. (#26)
- **`longhand redact`** — retroactive scan of an existing store. Reports pattern names and counts only (never the matched values); `--apply` masks matches in place across events, episodes, segments, and outcomes, and re-embeds changed vector documents. (#26)

### Fixed

- **Benign noise no longer counts as errors.** The error detector now skips known-noise lines — Next.js streaming-SSR `data-dgst`/`<!--$!-->` markers, "0 failing"/"Tests: 0 failed" summaries, empty `error:` fields, `Error: Task not found` tool churn, and missing-GNU-`timeout` probes — while continuing to scan, so real errors in the same output still register. This was the main inflator of the unresolved-episode rate. Run `longhand analyze --all` to re-extract episodes with the cleaner detector. (#25)

### Security

- chromadb is now capped `<1.0` for **all** Python versions (previously unbounded below 3.14) — a chromadb 1.x release can no longer break fresh installs. (#25)

### Internal

- mypy is clean (21 → 0 errors) and the CI typecheck job is now blocking. (#25)
- `ruff format` adopted repo-wide and gated in CI. (#24)
- 311 tests (280 → 311). The May 2026 audit report is archived under `docs/audits/`. (#24–#26)

## [0.9.4] — 2026-06-09

Hardening release: closes the six findings left open after the v0.9.3 security audit (AUDIT-2026-05-28). No new features, no API changes.

### Fixed

- **Parser no longer crashes on non-object JSON lines.** A transcript line that is valid JSON but not an object (a bare list, string, or number) raised `AttributeError` and silently lost the rest of the session. Both the full-parse and live-tail paths now skip such lines. (#22)
- **Ingest lock is now race-safe.** `claim_ingest_lock` used a check-then-write that let two racing processes both believe they held the lock. The lock is now claimed with an atomic `O_CREAT|O_EXCL` create; stale locks from dead PIDs are unlinked and re-claimed. (#22)

### Security

- **MCP input bounds tightened.** A negative `limit` could slip past the cap and become SQLite's unbounded `LIMIT -1`; `limit` is now clamped to `[1, 1000]` and `offset` floored at 0. Semantic queries are capped at 2,000 characters before reaching the ChromaDB ONNX encoder, across all query-taking tools. Local-only exposure either way, but defense-in-depth is cheap. (#22)

### Internal

- The `posthog<3.0` pin is now documented in `pyproject.toml`: it deliberately constrains chromadb's transitive telemetry client (posthog ≥3 breaks chromadb's `capture()` calls and floods stderr — the v0.5.11 fix). It is not a dead dependency; do not remove it.
- Un-skipped the fixless-episode forensic assertion in the test suite (it passes) and added coverage for the parser guard, lock atomicity, and MCP bounds. 273 → 280 tests.

## [0.9.3] — 2026-05-28

Bug-fix release: restores two features that were silently broken in Claude Code, plus CI/test/doc hardening.

### Fixed

- **Auto-context injection hook restored.** The `UserPromptSubmit` hook imported `context` from `longhand.cli`, which exports only `app` after the `cli.py` → `cli/` package split. The swallowed `ImportError` made the hook emit `{}` on every prompt — so automatic context injection had been silently dead since v0.9.0. (#11)
- **MCP tools now load in Claude Code.** 12 of 19 tools declared an `outputSchema` whose `type` was `array`/`oneOf`; Claude Code's MCP validator rejects any `outputSchema.type` that isn't `"object"`, so those tools silently failed to load. Handlers return text (never structured content), so the schemas were decorative — they are now stripped before tools reach the client. (#9)

### Changed

- `SECURITY.md` corrected to describe the two fixed-argv `subprocess` call sites and the single hardcoded-identifier `PRAGMA` f-string. The code was always injection-safe; the "no subprocess / zero f-string SQL" claims were stale. (#12)

### Internal

- CI now tests Python 3.14 (non-blocking — onnxruntime's cp314 wheel intermittently raises an illegal-instruction SIGILL on GitHub's heterogeneous runners), gates coverage at `--cov-fail-under=60`, and runs a non-blocking `mypy` job. (#13)
- Behavioral test backfill for the CLI and the recall time parser; total coverage 66% → 73%, `time_parser.py` → 100%. (#14)

## [0.9.2] — 2026-05-17

`longhand demo` — try Longhand on a sample corpus without touching your real history.

### Added

- **New `longhand demo` CLI command.** Generates a sandboxed store at
  `/tmp/longhand-demo-<timestamp>/`, seeds it with 3 fictional Claude Code
  sessions covering a realistic Stripe-webhook bug + Supabase auth migration
  + downstream 401 fix on a `demo-shop` project, then walks through `recall`
  and `recall_project_status` against the seeded store so the user can see
  what the output looks like before pointing Longhand at their own data.
  Cleans up afterwards; pass `--keep` to leave the sandbox in place for
  further exploration with `LONGHAND_DIR=<path> longhand …`.
- `longhand.demo` package containing the corpus generator
  (`longhand/demo/corpus.py`) and walkthrough runner
  (`longhand/demo/runner.py`). 6 new tests verify the corpus is
  deterministic, valid Claude Code event shape, ingests cleanly into a
  fresh store, and that recall returns sensible results against the seeded
  data.

### Why

The 2026-05-17 audit identified an onboarding gap: new users had no way to
preview Longhand on safe data before running `longhand setup` on their
real `~/.claude`. `longhand demo` closes that gap with a 60-second
walkthrough that exercises the cross-session recall, file history, and
project-status surfaces without modifying anything on disk outside the
sandbox.

### Tests

- 228 tests passing (was 222).

---

## [0.9.1] — 2026-05-05

Tool definition quality pass. Every MCP tool now serializes a human-readable
`title`, an `annotations` block (`readOnlyHint`, `openWorldHint`, plus
`idempotentHint` on `reconcile`), and — for the 17 tools whose response is
pure structured JSON — an `outputSchema` describing the response shape.

### Added

- `title`, `annotations`, and `outputSchema` on all 19 MCP tools. Two
  helper constants (`_READ_ONLY` and `_RECONCILE_HINTS`) at the top of
  `mcp_server.py` keep the per-tool blocks readable.
- Anti-pattern callouts in the descriptions of the four lowest-scoring
  tools (`get_stats`, `replay_file`, `get_project_timeline`,
  `get_session_commits`) and the borderline tools (`find_episodes`,
  `get_episode`, `find_commits`, `list_projects`, `list_sessions`,
  `get_file_history`, `replay_file`) — every tool description now names
  what NOT to use it for, making tool selection sharper.
- `server.json` description tightened to mention tool count and capability
  grouping.

### Why

GLAMA scores MCP tool definitions on six dimensions, with `outputSchema`
and `annotations` being two MCP-spec fields most directly tied to
"Behavioral Transparency" and "Contextual Completeness." This release adds
both across the surface so MCP clients (and ranking sites like GLAMA) can
treat each tool as documented metadata rather than free-form prose.

No behavior changes. No breaking schema changes.

---

## [0.9.0] — 2026-04-28

Live ingestion. Sessions are now ingested incrementally during the session
instead of only at SessionEnd, so a Claude Code crash can no longer wipe an
in-progress session's events from the index.

### Added

- **`Stop` hook + `longhand ingest-live` command.** Fires once per assistant
  turn. Tail-reads new bytes from the transcript JSONL and upserts new
  events into SQLite. Skips heavy analysis (episodes, segments, embeddings,
  project inference) — those still run at SessionEnd. The events table
  stays current within seconds of each turn, so an editor crash, a kernel
  panic, or a stuck SessionEnd subprocess no longer means lost work.
  `longhand hook install` now installs both SessionEnd and Stop in one go.
- **Plan history is now first-class.** Every Write/Edit to a
  `~/.claude/plans/*.md` file is captured as an event. The new
  `plans_index` SQL view (`longhand plans list` / `mcp_longhand_list_plans`)
  exposes them in chronological order. Pair with `replay_file` to
  reconstruct an early version of a plan that was later overwritten —
  previously, only the final plan was visible because the file on disk
  had been overwritten by the time SessionEnd fired.
- **`longhand schedule install-reconciler`** writes a launchd job
  (macOS) that runs `longhand reconcile --fix` every 30 minutes.
  Belt-and-suspenders for hard crashes that take down both hooks. Opt-in;
  `longhand doctor` flags it when missing.
- **MCP tool `list_plans`** for the Claude-side view of plan history.

### Changed

- **SQLite WAL mode** enabled at first connect. Cuts hook latency under
  contention from worst-case 5s (`busy_timeout`) to ~10ms. Single-user DB,
  safe.
- **`longhand hook uninstall`** now also removes the Stop hook.
- **`longhand doctor`** has new rows for Stop hook and Reconciler job.

### Schema

- Migration v5: adds `last_offset` column to `ingestion_log` (live cursor
  separate from `file_size`, which still tracks last-full-ingest size).
  Backfill: existing rows get `last_offset = file_size`. Adds
  `plans_index` view over `events`.

### Tests

- 11 new tests covering: stdin contract, offset advance, partial-line
  handling, lock contention, plans_index view, migration v5,
  live → SessionEnd composition, episodes-skipped invariant.
- 222 tests passing (was 211).

---

## [0.8.1] — 2026-04-23

Closes the staleness silent-failure class across MCP entry points and
exposes `reconcile` as an MCP tool so Claude can self-heal the index from
inside a session.

### Fixed

- **`search` and `list_sessions` now surface `stale: true / stale_reason`**
  when the project they're scoped to has on-disk transcripts not yet in the
  DB. Pre-v0.8.1 these tools returned clean-looking empty results — same
  failure shape `recall_project_status` was built to catch, just one layer
  up. Caught live tonight: `search("portneuf junk removal")` returned
  `hits: []` for a project whose only transcript existed on disk but
  hadn't been ingested yet. `recall_project_status` already reported
  staleness; now `search` and `list_sessions` do too.
- **`list_sessions` default `limit` raised from 20 to 50.** Active days
  routinely cross 5+ projects across 5+ sessions; the old default
  truncated reviews silently.

### Added

- **`reconcile` MCP tool.** Wraps `longhand reconcile --fix` so Claude can
  re-ingest missing transcripts in-session after a staleness banner fires —
  no shell-out required. Defaults to `fix=True` for MCP callers (CLI keeps
  `fix=False` default — dry-run summary). Same ingest lock as the CLI; safe
  under concurrent ingestion.
- **Shared reconcile core** at `longhand/recall/reconcile.py` with a
  `ReconcileReport` dataclass. CLI and MCP tool both call `run_reconcile`;
  only the presentation differs.
- **`staleness_banner()` helper** in `recall_pipeline.py` — thin wrapper
  over `_detect_project_drift` for any handler that needs the same
  drift signal `recall_project_status` returns. Cache-backed; cheap on
  repeat calls.

### Tests

- 6 new tests covering: search staleness on auto-scope, list_sessions
  staleness on project filter, reconcile dry-run, reconcile fix-ingests,
  reconcile MCP default of `fix=True`, dispatch count.
- 211 tests passing (was 205).

---

## [0.8.0] — 2026-04-23

Cleaner narratives + a real bug-finding test layer underneath.

### Fixed

- **`fix_summary` no longer leaks the literal `"Intent:"` label.**
  Pre-v0.8 `_compose_fix_summary` prepended `"Intent: "` to the assistant's
  intent text "for embedding structure" (per the original comment). The
  label leaked into every recall narrative for affected episodes. Audit on
  the reference corpus showed 100 of 204 episodes (49%) had this. Migration
  v4 strips it from existing rows on first store open; no command needed.
- **`fix_summary` diff content now truncates at clean boundaries.** The
  hard `[:120]` byte slice landed mid-token (`phoneNum'`, `family?:'`,
  `strin'`). New `_truncate_at_boundary` helper backs off to the last
  whitespace within budget and appends `…`. Forward-only — existing rows
  stay until reingestion.
- **Recall narrative footer "Other matches" lines now include the
  `session_id`.** Was silently hiding which session each match came from,
  leaving the user unable to drill in.
- **Recall now surfaces secondary segment matches** in an "Also possibly
  relevant" footer when episodes win the primary slot. Cross-session hits
  used to be dropped silently when episodes were the primary surface.

### Added

- **Canary harness** at `tests/canary_harness.py` +
  `tests/test_canary_corpus.py`. Auto-discovers fixture modules under
  `tests/fixtures/corpus/`. Default assertion mode is `"narrative"` (what
  users read), not internal arrays. Each canary pins a real shipped bug;
  new ones must prove teeth (fail before the fix, pass after) before
  landing. See `tests/fixtures/corpus/README.md` for the convention.
- **Real-corpus recall validator** at `scripts/recall_diff.py`. Snapshots
  top-N episode/segment IDs and narrative session prefixes for a fixed
  query list against `~/.longhand`, diffs against a saved baseline. Closes
  the gap pytest can't see — ranking shifts on real data.

### Changed

- Removed the `event_semantic_boost` ranking signal and its supporting
  `semantic_event_scores` step. The comment already called it the "older
  path, secondary signal"; `episode_semantic_boost` from the episode's
  own embedding distance is the modern equivalent. Net: 1 fewer ranking
  signal, 1 fewer vector-search call per `recall()`.
- Pulled the inline keyword-extraction regex + stopword list to a
  documented module-level helper (`_extract_query_keywords`).

### Migrations

- **v4** — strips leaked `"Intent: "` prefix from existing `fix_summary`
  rows. Auto-applies on next store open. Embeddings are not re-generated
  (semantic clusters of `"Intent: foo"` and `"foo"` are close enough that
  ranking is unchanged; the validator confirmed).

---

## [0.7.0] — 2026-04-22

Cleaner recall output, faster drift checks. Follow-ups to the v0.6.0 audit.

### Added

- **`longhand doctor` freshness check.** New "Recent ingest (7d)" row
  compares on-disk JSONL mtimes against the sessions table. Red ✗ with
  reconcile hint when most transcripts from the past week aren't indexed
  — catches the silent-hook-failure class of bug.
- **Drift-detection cache** at `~/.longhand/cache/jsonl_project_map.json`,
  keyed on `(transcript_path, mtime)`. Warm-call `recall_project_status`
  drops from ~2,333ms → ~68ms (34× speedup). Cold calls unchanged.
- **`search` auto-scopes to a matched project** when the query names a
  known project at confidence ≥0.8. Response wraps in
  `{auto_scoped_to, auto_scope_hint, hits}` so agents can override.

### Fixed

- Narrative drops commits with no parseable hash at three layers
  (extractor returns `None`, SQL filters them out, narrative skips any
  that slip through). No more empty backticks in the rendered output.
- "Last session" trailer in `recall_project_status` now sources from the
  most-recent episode's `fix_summary` instead of `session_outcomes.summary`
  (which was the first user message of the session, not a fix description).

---

## [0.6.0] — 2026-04-22

Recall sees sessions it previously missed. Driven by a dogfood test where
`recall_project_status("bsoi-mesh-kit")` returned "No session history found"
despite four real transcripts on disk.

### Fixed

- **Project inference no longer uses first-event `cwd` only.** Sessions
  that launch from `$HOME` and `cd` into a project mid-session were
  losing attribution (project_id → NULL). `build_session` now tallies
  all event cwds, filters out `$HOME` and paths without project markers,
  and picks the mode.
- **Subagent JSONL transcripts under `*/subagents/*.jsonl` are no longer
  treated as top-level sessions.** Pre-existing bug exposed by the new
  `reconcile` command — subagent files were being re-ingested as
  standalone sessions, double-counting totals.

### Added

- **`longhand reconcile [--fix]`** command. Walks `~/.claude/projects`,
  buckets transcripts into fully-indexed / NULL-project / missing, and
  can re-ingest the problem rows using the improved inference. Closes
  the silent-hook-failure recovery loop.
- **`recall_project_status` drift detection.** New fields:
  `session_count_indexed`, `session_count_on_disk`, `last_ingested_at`,
  `last_transcript_mtime`, `stale`, `stale_reason`. When stale, the
  narrative is prepended with `⚠` pointing at `reconcile --fix`.

---

## [0.5.13] — 2026-04-20

Audit cleanup bundle. Five small independent changes, no API impact.

- Close FD leak in `trigger_background_ingest` (parent FD now closes once
  Popen has duplicated it into the child).
- Capture batched-embedding return counts in `analyze_session` so callers
  see how many vectors actually landed in Chroma.
- Fold v1 migration columns into the base schema (cleaner fresh-install
  path).
- Dedupe `_resolve_session_prefix` (had two implementations).
- Introduce `CHROMA_BATCH_SIZE` constant (was a scattered magic number).

---

## Earlier releases

For v0.5.12 and earlier, see the annotated git tags:

```bash
git tag -l 'v0.*' --sort=-v:refname
git show v0.5.12          # tag annotation has the release notes
```

Highlights:

- **v0.5.12** — Large-corpus ingest performance improvements.
- **v0.5.x** series — Iterative quality fixes after v0.5.0 went on PyPI
  (2026-04-14, the first public release).
- **v0.5.0** — First PyPI release. SQLite + Chroma, episode extraction,
  fuzzy recall, MCP server, hook installer.
- **v0.4.x** — Pre-PyPI. Local-only iteration on episode extraction,
  segment search, narrative composition.
- **v0.3.x** — Pre-PyPI. Compare/check/repair CLIs.
- **v0.2.x** — Pre-PyPI. Proactive memory layer (project inference,
  outcome tagging, episode pairs).
- **v0.1.x** — Pre-PyPI. Initial parser + SQLite + Chroma scaffolding.
