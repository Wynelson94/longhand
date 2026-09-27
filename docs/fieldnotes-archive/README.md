# Fieldnotes archive

These six notes were maintained by [fieldnotes](https://github.com/Wynelson94/fieldnotes),
which pinned each one to a SHA of the code it described and gated commits when the
pinned code moved. Fieldnotes was deactivated on this machine on 2026-09-17, so nothing
re-pins these any more. They are kept here verbatim because their content is still
correct and still worth not reintroducing.

## Why it was deactivated

The `fieldnotes` console script outlived its package — an orphaned shim. The pre-commit
hook tests for the tool with `command -v … || exit 0` to stay out of the way of
contributors who don't have it, but a shim that exists and fails is not the same as a
tool that is absent: `command -v` succeeded, the hook ran the script, the script died
with `ModuleNotFoundError`, and the hook reported **"a note is stale."** Every commit in
this repo was blocked by that false reading, and the message pointed at the notes rather
than at the tool.

## State at archival, verified against the code

Every claim below was checked against the source on 2026-09-17. All six hold. The notes
were mechanically stale — `parser.py` changed 2026-09-07 after 0003 was pinned in July,
`episode_extraction.py` changed 2026-07-10 after 0004 was pinned in June — but the prose
never drifted from the behavior. Re-pinning was all they ever needed.

| Note | Claim | Verified in |
|---|---|---|
| 0001 | Any user-facing query tool must surface staleness | `staleness_banner` in `longhand/recall/recall_pipeline.py` |
| 0002, 0006 | Project attribution uses the **mode** of cwds, not the first event's | `Counter(...).most_common(1)` in `longhand/parser.py` |
| 0003 | `*/subagents/*.jsonl` are not independent sessions | `_is_subagent_transcript()` in `longhand/parser.py` |
| 0004 | Never prepend a label (`Intent:`) to `fix_summary` | `_compose_fix_summary` in `longhand/analysis/episode_extraction.py` |
| 0005 | Truncate at a whitespace boundary and append `…` | `_truncate_at_boundary` in `longhand/analysis/episode_extraction.py` |

## Note 0003 is load-bearing

0003 documents that subagent transcripts are not their own sessions — they are ingested
under the parent `session_id`, each restarting its own `sequence` at 0. v1.2.1 fixed the
read-side consequence of exactly that: the shared server's `get_session_timeline` ordered
by `sequence` alone, so independent subagent threads interleaved and read as though rows
from several different sessions had been mixed together. The note describes the ingest
side; that fix describes what it costs on the way back out. Keep them together.
