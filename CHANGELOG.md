# Changelog

All notable changes to Longhand are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

For pre-v0.6 releases, the canonical record is the annotated git tag —
`git show v0.5.13` etc. Entries below are reverse-engineered from the
commits and tag annotations of those releases.

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
