# Git-backed backup and restore of the archive — design

Date: 2026-10-29. Status: approved. Target release: 1.3.0.

## Intent

Longhand's archive lives in one SQLite file. For most sessions it is the only
copy: Claude Code rotates transcripts off disk, so on the author's machine
792 of 879 sessions no longer exist anywhere but `~/.longhand/longhand.db`.
A dead disk or a new computer takes the history with it.

`longhand backup` sends the archive to a git remote the user owns, and
`longhand restore` brings it back on another machine. Both are opt-in:
nothing leaves the machine until the user runs the enable command, reads
what will be uploaded, and confirms. The feature is for every Longhand
user, not just the author.

## Goals, non-goals, anti-goals

Goals: the whole archive (Claude Code and Codex sessions, plus
`config.json`); any git remote; incremental runs; two machines sharing one
backup; a one-command restore; secret-shaped strings never leave the
machine in the clear unless the user says so.

Non-goals (follow-ups): encryption at rest; non-git targets; backing up the
vector index or derived tables (all re-derived on restore); pruning history
from the backup repo; Git LFS.

Anti-goals (a failure even if the code works): a push to a public repo; a
silent background upload the user never enabled; a backup that quietly
drops a session it once held; an irreversible change to the local store as
a side effect of backing up.

## Constraints

- GitHub limits: files over 100 MiB are blocked, files over 50 MiB warn, a
  push is capped at 2 GB, directories should stay under 3,000 entries.
- `git` must be on PATH; `gh` is optional (GitHub visibility check and
  `--create`). Longhand never stores a token: the machine's existing git
  credentials do the pushing.
- Every subprocess call is a list-form argv, never a shell string
  (SECURITY.md "Subprocess use"). No new dependencies.
- Paths must be valid on Windows (no `:` in file names).
- Secret values are never printed or logged: counts only.
- README, CLAUDE.md and the CLI must stay consistent
  (`tests/test_surface_consistency.py`).

## Measured facts (the author's store, 2026-10-29)

| Measure | Value |
|---|---|
| Sessions | 879 (6 Codex) |
| Sessions whose transcript is gone from disk | 792 |
| `longhand.db` | 6.8 GB |
| Verbatim record (`events.raw_json`) as stored | 5.12 GB |
| Same record, one copy per transcript line | 2.62 GB |
| gzip ratio on a 20k-line sample | 37% (about 1 GB packed in git) |
| Sessions over 50 MB after dedupe | 7 |

The database file itself cannot go to GitHub. The backup is an export of
the verbatim record, which Longhand already holds per event in
`events.raw_json`.

## Decisions

1. **Plain text in a private repo.** Readable on the host, git diffs work,
   restore is the existing ingest path. A public GitHub remote is refused.
2. **Manual, plus optional auto.** `longhand backup` on demand. A config
   switch lets the existing 30-minute reconciler push, at most once an hour,
   only when something changed. Auto mode never prompts.
3. **Secrets are masked in the backup copy only.** Every match of the
   `longhand redact` patterns is replaced in the exported copy with the same
   `first4…last4 (len=N)` mask. The local store stays verbatim. Nothing is
   held back. `--no-mask` exports verbatim and requires a typed
   confirmation. (Holding flagged sessions instead was measured first: it
   would have kept back 47% of the author's archive by size, most of it on
   card-shaped numbers the Luhn filter accepts.)

Assumptions: any git remote URL or local path works; Claude and Codex
sessions are both included; `config.json` is included; the Chroma index
and derived analysis are not.

## Design

### Repo layout (`backup.path`, default `<data_dir>/backup/`, a normal clone)

```
README.md                      what this is, how to restore
manifest.json                  {"format": 1, "longhand_version", "updated_at", "sessions": N}
config.json                    copy of <data_dir>/config.json when present
sessions/claude/YYYY/MM/<session_id>/session.json
sessions/claude/YYYY/MM/<session_id>/transcript.jsonl        (or transcript.part-0001.jsonl …)
sessions/codex/YYYY/MM/<thread_id>/session.json
sessions/codex/YYYY/MM/<thread_id>/rollout.jsonl             (or rollout.part-0001.jsonl …)
```

- `YYYY/MM` from `sessions.started_at` (UTC); an epoch-zero `started_at`
  goes under `undated/`. Codex directory names drop the `codex:` prefix;
  any character outside `[A-Za-z0-9._-]` becomes `_`. `session.json` holds
  the real session id.
- `session.json`: the `sessions` row, plus `format`, `longhand_version`,
  `session_id`, `source` (`claude`|`codex`), `rel_dir`, `parts` (ordered),
  `lines`, `bytes`, `sha256` of the concatenated transcript, `event_count`
  and `last_event_timestamp` at export, `exported_at`, `masked`,
  `original_transcript_path`.
- **Transcript reconstruction.** For one session, stream
  `SELECT raw_json FROM events WHERE session_id = ? ORDER BY timestamp, sequence, rowid`
  (the order `get_events` uses), keep the first occurrence of each distinct
  `raw_json` (sub-events split from one line share it, as do `#`-suffixed
  collision rows), write one line each, UTF-8, `\n` newlines
  (`core.autocrlf=false` in the repo). The Codex `session_meta` record is a
  stored event, so it comes back as the first rollout line.
- **Parts.** Split at 40 MB on a line boundary. A single line longer than
  that becomes its own part.
- **Restore contract: the lines, not the rows.** Restore re-parses the
  export with the current parser, exactly as `analyze --all` re-extracts
  with current extractors and `db vacuum --prune-aux` discards aux rows.
  Lines that older parser versions turned into events but
  `KNOWN_SKIP_ENTRY_TYPES` now skips (`attachment` and other aux types)
  stay in the backup verbatim; they do not become rows again. Event ids can
  differ from the originals, and `raw_json` is a re-serialization, so
  restored lines are the same JSON values, not the same bytes.
- **Change detection, two tiers, never shrink.**
  1. `<data_dir>/backup-state.json` (local, never in the repo) records per
     session `(event_count, max(timestamp), ingested_at, sha256)` as of the
     last run. One `GROUP BY session_id` query finds sessions whose triple
     moved; only those are exported to staging and hashed.
  2. If the staged sha256 equals the repo's `session.json.sha256`, the repo
     is untouched. If the staged line set is a subset of the repo's, the
     repo is untouched (a restored machine re-parses fewer lines than the
     original stored and must not overwrite the fuller copy). Otherwise the
     union is written: repo order first, local-only lines appended in local
     order. A session file in the repo only ever grows.
  `restore` seeds the state file with every session it imported, so a
  restored machine that keeps backing up re-exports nothing until a session
  actually changes.
- **Not backed up, by design:** `file_snapshot` events whose `session_id`
  is empty (the parser reads `sessionId` from a line that lacks it). They
  have no session to belong to; separate parser issue.

### Commands

- `longhand backup init --remote <url> [--path <dir>] [--create] [--yes]`
  1. `git` on PATH, else a plain refusal.
  2. Remote accepted: `https://`, `ssh://`, `git@host:path`, `file://`, an
     absolute local path. Refused: anything starting with `-`, any `::`
     transport-helper form. Every git call runs with
     `-c protocol.ext.allow=never` and `--` before paths.
  3. Privacy: a github.com remote with `gh` available must report
     `private` (`--create` runs `gh repo create … --private` first); a
     public GitHub remote is refused with no override. When visibility
     cannot be determined (no `gh`, another host), the user types `private`.
  4. Consent screen, then the user types `yes`: session counts, upload size
     (the deduped bytes, measured by the same pass the export uses), the
     remote, that the content is verbatim transcripts including thinking,
     tool output and file contents, that nothing is encrypted, the mask
     counts the scan found, and the current `redact.enabled` state. No TTY
     and no `--yes` → refuse. `--yes` covers the consent and the privacy
     attestation, never the public-repo refusal.
  5. Clone the remote if it has commits, else `git init` + `remote add
     origin`. An existing clone at the path is kept.
  6. Only then write `backup.enabled=true`, `backup.remote`, `backup.path`,
     `backup.auto=false` to `config.json`.
- `longhand backup [--dry-run] [--no-push] [--no-mask]`
  1. Refuse unless `backup.enabled`. `--dry-run` prints sessions to
     add/update, bytes, and mask counts per pattern.
  2. Commit anything an interrupted run left in the working tree, then
     `git fetch` and rebase onto the remote branch when it moved (another
     machine). A rebase conflict aborts the run; the never-shrink rule
     settles any session both machines touched.
  3. Export changed sessions to a staging directory with masking applied,
     reconcile each against the repo (new / unchanged / subset / grown),
     record its state entry.
  4. Commit and push in batches of at most 500 MB exported (about 190 MB
     packed) so each push stays far under GitHub's cap and a mid-run failure
     keeps earlier batches safe. Commit messages carry counts only.
  5. Save the state file (`last_run_at`, `last_push_at` only when every
     push succeeded, `last_error`, mask counts). Exit non-zero when a push
     failed. Progress via `rich.progress`, same as `ingest`. No pace
     estimates.
- `longhand backup status`: enabled/auto, remote, path, last run and push,
  pending sessions (cheap filter), last run's mask counts, repo size.
- `longhand backup auto on|off`: writes `backup.auto`.
- `longhand backup disable`: `backup.enabled=false`; the clone stays.
- `longhand restore [--from <url>|<path>] [--data-dir] [--skip-analysis]`
  1. Clone when `--from` is a remote and the path has no repo; a `--from`
     path that is already a clone is used in place.
  2. Read every `session.json` (manifest `format` must be ≤ the one this
     version writes). Skip a session whose sha256 the local state file
     already records. Never compare event counts.
  3. Concatenate the parts and parse with `JSONLParser` (it recognises the
     Codex `session_meta` line and switches to the Codex adapter itself),
     then `LonghandStore.ingest_session(session, events, run_analysis=not
     skip_analysis)` under the ingest lock. `transcript_path` is set to the
     first part inside the clone: a stable path that exists.
  4. `config.json` is copied back only when the local one is absent.
  5. Exit text: counts, then the hint to run `longhand backup init
     --remote …` on this machine to keep backing up.
- **Auto mode:** at the end of `reconcile --fix` (what the launchd
  reconciler runs every 30 minutes): when `backup.enabled` and
  `backup.auto`, at least 3600 s since `last_push_at`, and the cheap filter
  finds changes, run the backup in-process with masking on. Failures go to
  `<data_dir>/logs/backup.log` and never raise.
- **doctor:** one `Backup` row: `off` / `on · last push <age>` / `auto ·
  last push <age>` / `on · never pushed` / `on · last push FAILED <age>`.

### Masking

The exporter runs the `longhand.redaction` patterns over each line before
it is written: a cheap scan first, and only lines with a hit are parsed,
masked leaf by leaf (`_redact_obj`), and re-serialized with the same
`json.dumps` call the store uses, so an unmasked line is byte-identical to
the stored one. A line that is not valid JSON is masked as text. Masking is
deterministic, so the content sha is stable. Per-pattern counts are
reported; values never are.

### Docs and promises that change

- README principle 8 ("No sync") gains the one opt-in exception, disclosed
  in full. Quick Start and a Backup section name the commands; a
  `docs/backup.md` page covers setup, what is uploaded, masking,
  multi-machine use, restore, limits and what is not backed up.
- SECURITY.md "Subprocess use" lists the third call site with its argv
  shapes, the remote validation and the `protocol.ext.allow=never` guard;
  the "paths Longhand writes to" list gains `<data_dir>/backup/`,
  `<data_dir>/backup-state.json`, `<data_dir>/logs/backup.log`.
- CHANGELOG: Unreleased → 1.3.0.

## Left to the builder

Temp-file concatenation versus a multi-path parser; the exact wording of
the consent screen beyond the items listed; how the staging directory is
named; repo-local git identity for commits when none is configured
globally; the rebase retry when a push is rejected because the remote moved
during the run.
