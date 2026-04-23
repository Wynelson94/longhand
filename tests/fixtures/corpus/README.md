# Canary corpus

Layer 1 of Longhand's bug-finding test suite. One fixture module per
real-world bug we never want to reship.

## Why this exists

Synthetic unit tests pass even when recall silently drops user-visible
results. Every canary here is a real bug that shipped (or almost shipped)
because the existing test suite couldn't see it. Adding a canary for each
bug class means:

- A new bug → write a canary that fails → fix it → canary passes → it can
  never come back without re-failing the suite.
- Tests assert against the **rendered narrative** (what users actually see),
  not internal data structures (which can be populated correctly while the
  user sees nothing).

## Convention

Each canary is a Python module named `canary_*.py` in this directory. The
harness (`tests/canary_harness.py`) auto-discovers them via
`pkgutil.iter_modules`. Each module must expose three names:

```python
DESCRIPTION = "One paragraph describing the bug class this captures."

SESSIONS = [
    ("filename_a.jsonl", [event_dict_1, event_dict_2, ...]),
    ("filename_b.jsonl", [event_dict_1, ...]),
]

ASSERTIONS = [
    RecallAssertion(
        query="...",
        must_surface_session_id="...",
        # default must_appear_in="narrative" — assert against rendered
        # markdown, not internal arrays. Override only when testing a
        # specific data path.
        description="...",
    ),
]
```

Each session is written to disk as JSONL by the harness, parsed by Longhand's
real `JSONLParser`, and ingested via `LonghandStore.ingest_session()`. No
mocks. The store is a temp directory — fresh per test.

## Validating a canary has teeth

When adding a new canary, **prove it fails before the fix lands**. Otherwise
you have a green test that may not test what you think it tests. The
prove-teeth procedure:

```bash
# 1. Write the canary first
git stash push longhand/  # stash the fix
pytest tests/test_canary_corpus.py::test_canary -k "<canary_name>" -v
# expect FAILED

# 2. Restore the fix
git stash pop
pytest tests/test_canary_corpus.py::test_canary -k "<canary_name>" -v
# expect PASSED
```

If the canary passes pre-fix, the assertion is too loose — tighten it
(usually means asserting against `narrative` rather than internal arrays).

## Where to assert

- **`narrative`** (default) — what users see. Use this 95% of the time. If
  a session is in `result.segments` but doesn't appear in the rendered
  narrative, the user can't see it. That's the bug.
- **`episodes` / `segments`** — internal data paths. Only use when testing
  the extraction or storage layer specifically.
- **`footer`** — the "Also possibly relevant" footer specifically. Use when
  the bug is "this should appear as a secondary surface."
- **`any_data`** — debug-only. Loosest possible. Don't use in canaries.

## Existing canaries

| File | Bug class |
|---|---|
| `canary_recall_secondary_match.py` | Mixed-topic session loses its visibility in the narrative when a more focused session also matches. Real-world: `eef364a9` ("v0.8 inside v0.7.0 release notes draft", April 2026). |
| `canary_project_inference_home_cd.py` | Session that launches in $HOME and `cd`s into a project mid-session was attributed to NULL, causing "No session history found" for projects that clearly had transcripts. Real-world: bsoi-mesh-kit invisible to recall_project_status (April 2026, fixed in v0.6.0). |

## Sibling regression tests (different shape, same intent)

Some bugs don't fit the canary harness shape but are still real-bug regressions
worth pinning. Convention: write a normal pytest test, name it
`test_<bug-class>_<short-description>`, and reference the original bug in the
docstring (release version, what shipped to fix it).

| Test | Bug class |
|---|---|
| `tests/test_parser.py::test_discover_sessions_filters_subagent_jsonls` | Subagent JSONLs under `*/subagents/` were treated as top-level sessions, double-counting and re-ingesting on reconcile (fixed in v0.6.0). |
