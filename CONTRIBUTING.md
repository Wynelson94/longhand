# Contributing

Longhand is a personal-memory tool that runs on every Claude Code turn. That shapes what "done" means here more than any style guide would: a change that is correct but occasionally blocks someone's prompt is not an improvement.

## The gate

Every pull request must pass, and these are required checks on `main`:

```
ruff check longhand tests
ruff format --check longhand tests
mypy longhand
pytest
```

Tests run on Python 3.10 through 3.14. A Windows leg (`windows-latest × py3.12`) runs non-blocking as evidence, not as a gate.

Work on a branch and open a PR — `main` is protected and takes no direct pushes.

## Before you change the schema

Read the **migration authoring policy** in the header of `longhand/storage/migrations.py`. It is not style advice; it is what makes Promise 2 in [COMPATIBILITY.md](COMPATIBILITY.md) true. The short version:

- Migrations are automatic, one-time, and **never renumbered** — a published version number is frozen forever, content included.
- Anything beyond O(1) DDL does not run inline at store open. Store open is on the hook path.
- Nothing destroys data without an explicit opt-in.
- Every new migration ships a **prior-schema fixture test** under `tests/fixtures/db/` — a real dump, not a hand-written schema.

## Before you change the hooks

Read `tests/test_hook_guarantees.py` first. Hooks never raise, never touch the network, and never block the prompt (Promise 3). A hook that fails must exit 0, leave a breadcrumb, and let `reconcile` clean up later. If your change can make a hook slow, raise, or reach the network, it needs a different design rather than a passing test.

## Before you change the CLI or MCP surface

Promise 1 freezes the surface through 1.x. Additive changes — new commands, new tools, new optional parameters, new response fields — are always fine. Removals and renames are 2.0 material and must warn one full minor ahead first.

Retired MCP tool names stay in `_DISPATCH` **forever**. Users paste tool names into their own `CLAUDE.md` files; those files are not ours to update, so a retired name answers with a migration preamble rather than failing.

## Tests

Write the test first and watch it fail. A test that has never been red has not been shown to test anything.

Prefer a test that pins the behavior a user depends on over one that pins the current implementation. When a test encodes a decision, say why in the test or a comment near it — the reason is the part that gets lost.

## Reporting a bug

Include what `longhand doctor` prints. It covers store health, freshness, hook errors, and transcript-format drift, which is most of what a diagnosis needs. Redact paths if they are sensitive — Longhand indexes your real work.
