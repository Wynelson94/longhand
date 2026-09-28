# Compatibility promises

Longhand 1.0 is a commitment, not a version bump. This document is that commitment: five promises, each with a named artifact in the repo that enforces it. A promise without an enforcement artifact is a wish, so every one below points at code you can read and a test that fails if the promise breaks.

Scope: these promises bind all of **1.x**. Breaking any of them requires 2.0.

---

## 1. Stable surface

The CLI commands and MCP tools shipped at 1.0 keep working through 1.x. Removals and renames happen only at a major version, and anything slated for removal warns for **one full minor** first.

**Enforced by:** the 0.13 deprecation cycle (every 1.0 removal warned in 0.13, one full minor ahead), the retired MCP names, which left `list_tools()` at 1.0 but keep answering from `_DISPATCH` **forever** with a migration preamble — see `_RETIRED_TOOLS` in `longhand/mcp_server.py` — and `tests/test_surface_consistency.py`, which fails CI if a retired name leaks back into the listing, stops dispatching, or a tool description points at one (plus `tests/test_mcp_tools.py:64-79` exercising the surviving call shapes directly).

That last part is deliberate. Users paste tool names into their own `CLAUDE.md` files, and those files are not ours to update. A retired name must never hard-fail; it answers, tells you what replaced it, and does the work anyway.

**Additive changes are always allowed:** new commands, new tools, new optional parameters, new fields in a response. The 1.1 surface added under this promise — `codex-sync` and its flags, and the `longhand-shared` server's four tools — is bound by it the same as everything from 1.0.

## 2. Forward data compatibility

Any database written by 0.11 or later opens on any 1.x release at least as new as its last writer. Migrations are automatic, one-time, and never renumbered.

The promise is **forward-shaped**, and the distinction matters: older code does not silently tolerate a newer database — it **refuses loudly**, with an instruction to upgrade. Operating blind on a schema you do not understand is how data gets corrupted quietly. Refusing is the feature.

**Enforced by:** `SchemaTooNewError` in `longhand/storage/migrations.py` (the downgrade guard), the migration authoring policy in that module's header, and a 0.11-schema fixture database under `tests/fixtures/db/` that is loaded, migrated, and queried in the test suite.

## 3. Hook guarantees

Longhand's Claude Code hooks **never raise, never touch the network, and never block your prompt**.

This is the promise that matters most in daily use, because a hook runs on every turn. A memory tool that occasionally breaks your editor is worse than no memory tool. All three hooks exit 0 on failure, but they don't fail identically: `SessionEnd` (`ingest-session`) writes a one-line breadcrumb to `logs/hook-errors-YYYY-MM-DD.log` and leaves the transcript for `reconcile` to pick up; `Stop` (`ingest-live`) swallows a failure with no breadcrumb at all — it runs on every turn, so it stays silent rather than adding I/O to that path, and `SessionEnd`/`reconcile` are the backstop; `UserPromptSubmit` (`__prompt-hook-run`) prints `{}` and injects nothing.

"Never touch the network" has one shared, disclosed exception: the first time a hook needs the local embedding model and it isn't cached yet, ChromaDB downloads it (~80MB, one time, from a fixed URL — not pypi.org). No hook makes the interactive CLI's update-check request; that one is structurally excluded (see SECURITY.md).

**Enforced by:** `tests/test_hook_guarantees.py`, CI-gated on every PR.

**Field record:** across the v0.13 bake (2026-07-11 → 08-12) there were 23 `SessionEnd` hook failures — the only hook that leaves a breadcrumb, so the only one this count can see. Every one exited 0, left a breadcrumb, and never blocked a prompt.

## 4. Upstream drift is never silent

Claude Code's transcript format is not ours to control — and neither is Codex's, since 1.1.0. When an unknown entry type appears, Longhand **preserves it** (stored as `raw_json`), **surfaces it** (the "Transcript format" row in `longhand doctor`), and **regression-gates it** (a fixture test). It does not drop data it does not recognize, and it does not pretend nothing changed. Codex's `response_item`/`event_msg` envelopes are unwrapped one level so the row names the actual kind (`response_item/<kind>`, `event_msg/<kind>`) instead of the generic envelope type.

**Enforced by:** the doctor row; `tests/fixtures/transcript_shapes/` + the raw_json preservation path in the parser for Claude Code; `tests/fixtures/codex_shapes/` + `tests/test_codex_shapes.py` for Codex, covering both CLI generations' rollout shapes.

### raw_json storage compatibility

Readers accept both the inline and normalized forms of preserved entries, indefinitely. A future 1.x may normalize `raw_json` storage into a dedicated `entries` table for deduplication — that change is **additive and opt-in**, both forms stay readable, and no existing database is rewritten without an explicit command. Nothing about that work can break a database written by an earlier 1.x.

## 5. Honest metrics

Error, fix, and resolved counts reflect real signals. Longhand does not inflate what it found, and it does not recommend a remedy that cannot work.

**Enforced by:** the verification gate and context-aware error suppression in `longhand/extractors/errors.py` (which cut optimistic bias from both directions — benign noise no longer becomes a "problem," and real errors are suppressed by context rather than by deleting patterns), the class-aware hook-error remedy in `_hook_errors_status()`, and the runtime-text checks in `tests/test_surface_consistency.py` that fail CI if `setup`, a tool description, or a recall narrative ever suggests a command or tool that doesn't exist.

That last one earned its place. Through 0.13, `doctor` told every hook error to run `reconcile --fix`. But `reconcile` enumerates from **disk**, so a transcript that never landed is invisible to it forever — the advice was a no-op for that entire class. Over the bake, **21 of 23** real hook errors were exactly that class. The row now splits the remedy by class and says plainly when there is nothing to heal.

---

## What is explicitly *not* promised

- **Episode extraction quality.** Episodes are a heuristic over your transcripts, not a guarantee. Precision and recall may change between minors as the extractors improve.
- **Performance characteristics.** Ingest and recall latency may change in either direction.
- **The Chroma vector index on disk.** It is a derived cache. Any release may require a re-index; your SQLite store is the source of truth and is covered by Promise 2.
- **Python versions below the floor in `pyproject.toml`.** Dropping an end-of-life Python is a minor-version change, not a major one.
- **Windows.** CI-tested on a best-effort basis (`windows-latest × py3.12`, non-blocking). Not a supported tier — see the README for the current evidence.
