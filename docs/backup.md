# Backing the archive up to git

Longhand keeps your whole history in one SQLite file. On most machines that
is the only copy: Claude Code deletes old transcripts, and most sessions in a
mature archive no longer exist anywhere else. `longhand backup` writes the
verbatim record of every session into a git repository you own and pushes
it, so a crashed or replaced computer does not take the history with it.
`longhand restore` brings it back.

It is opt-in. Nothing leaves this machine until you have read what would be
uploaded and typed `yes`. Needs Longhand 1.3.0 or newer, and `git` on your
PATH.

## Set up in three commands

```sh
longhand backup init --remote git@github.com:you/longhand-backup.git   # consent screen, then type yes
longhand backup --dry-run                                               # the plan: sessions, bytes, what gets masked
longhand backup                                                          # the first upload
```

`init` validates the remote, checks that it is private, measures the archive,
and prints exactly what would be uploaded. For example:

```
Longhand backup — what leaves this machine

  Remote:        git@github.com:you/longhand-backup.git
  Repo clone:    /Users/you/.longhand/backup
  Sessions:      879 (873 Claude Code, 6 Codex)
  Upload size:   2.4 GB of transcript text (about 924.6 MB once git compresses it)
  Content:       verbatim transcripts — prompts, thinking, tool output, file contents
  Encryption:    none (whoever hosts the remote can read it)
  Secrets:       secret-shaped strings are masked in the copy — github_token: 9, ssh_private_key: 7 (14 sessions)
  Local store:   unchanged (redact.enabled = False)

Nothing is uploaded until you type yes.
```

Then it creates the clone (or clones the remote when it already has commits)
and writes `backup.enabled` to `~/.longhand/config.json`. Nothing has been
uploaded yet; `longhand backup` does that.

`--remote` accepts `https://…`, `ssh://…`, `git@host:path`, `file://…`, or
an absolute path (an external drive or a NAS mount). A remote that starts
with `-` or names a transport helper (`ext::`) is refused. `--path` puts the
clone somewhere other than `<data dir>/backup`. `--yes` accepts the consent
text without a prompt, for scripts; without a terminal and without `--yes`,
`init` prints the text and refuses.

### Private only

A GitHub remote must be private. When `gh` is installed and signed in, `init`
asks it; a public or organization-internal repository is refused outright.
`--create` runs `gh repo create <owner>/<repo> --private` first. When the
visibility cannot be checked (no `gh`, or a remote that is not github.com),
`init` says so and asks you to type `private` as well.

## What is uploaded

- **Every session's transcript, verbatim**, reconstructed line by line from
  what Longhand stored: prompts, thinking blocks, tool calls and their
  output, the full before and after of every file edit. Claude Code sessions
  and Codex rollouts both.
- **`config.json`**, minus the `backup` section (the clone path is
  machine-specific).
- A `README.md` explaining the layout and how to restore, and a
  `manifest.json` with the format version and the session count.

Nothing is encrypted. Whoever can read the remote can read your sessions.
Encryption at rest is a follow-up; until then, treat the repository like the
archive itself.

Layout:

```
sessions/claude/2026/04/<session-id>/session.json
sessions/claude/2026/04/<session-id>/transcript.jsonl        (or transcript.part-0001.jsonl …)
sessions/codex/2026/09/<thread-id>/session.json
sessions/codex/2026/09/<thread-id>/rollout.jsonl             (or rollout.part-0001.jsonl …)
```

Sessions are bucketed by the month they started (`undated/` when the start
time is unknown). Transcripts over 40 MB are split into parts on line
boundaries, under GitHub's 50 MiB warning and well under its 100 MiB block.
The first upload is committed and pushed in batches of about 500 MB of
transcript text, so a long upload that is interrupted keeps what it had
already pushed.

### Secrets are masked in the copy

Every string matching one of `longhand redact`'s patterns (API keys, GitHub
and Slack and Stripe tokens, AWS keys, private-key headers, database URLs
with passwords, JWTs, SSNs, card numbers that pass the Luhn check) is
replaced in the uploaded copy with the same mask `longhand redact` uses:
`ghp_…AAAA (len=40)`. The local store is never changed. The consent screen,
`--dry-run`, and every run print counts per pattern and the number of
sessions touched; values are never printed.

`longhand backup --no-mask` uploads verbatim. It needs a terminal and asks
you to type `verbatim`. Mixing masked and verbatim runs leaves near-duplicate
lines for the same event in the repository; on restore the later one wins.

## Day to day

```sh
longhand backup            # export what changed, commit, push
longhand backup --no-push  # commit into the clone only
longhand backup status     # remote, clone, pending sessions, last push, last error, masks
longhand backup auto on    # let the scheduled reconciler push, at most once an hour
longhand backup disable    # off; the clone and the remote keep what was pushed
```

Only sessions whose event count, last event, or ingest time moved since the
last run are exported. A live-tailed session grows between runs and is
re-exported; git stores the difference.

**Auto mode** runs at the end of every `longhand reconcile --fix` pass, which
is what `longhand schedule install-reconciler` schedules every 30 minutes on
macOS. It backs up at most once an hour, only when something changed, with
masking always on. Network and git failures go to
`~/.longhand/logs/backup.log` and never affect the reconciler. `longhand
doctor` shows a `Backup` row: `off`, `on · last push 12 min ago`,
`auto · last push 2 h ago`, or `on · last push FAILED 3 h ago`.

**A failed push** is reported, not raised: the commits stay in the clone and
the next run pushes them. `backup status` shows the unpushed count.

### More than one machine

Every run fetches first and rebases local commits onto the remote. A session
in the repository only ever grows: when two machines hold different copies
of the same session, the union is written (the repository's lines first,
then the lines only this machine had), and a copy that is a subset of what
the repository holds changes nothing. A restored machine that keeps backing
up therefore adds nothing until a session actually changes. A rebase
conflict ends the run with the error in `backup status`; resolve it in the
clone like any git conflict.

## Restore

```sh
pip install -U longhand
longhand restore --from git@github.com:you/longhand-backup.git --skip-analysis
longhand backup init --remote git@github.com:you/longhand-backup.git   # keep backing up from here
```

`restore` clones the remote into `<data dir>/backup` (or uses a clone you
point it at), parses every transcript with the current parser, and ingests
it under the ingest lock. Vectors and the derived tables (episodes,
segments, outcomes, projects) are rebuilt by the ingest pipeline;
`--skip-analysis` defers the analysis so you can run `longhand analyze --all`
later. `config.json` is restored only when this data dir has none. A second
`restore` imports nothing: sessions are skipped by content hash.

What comes back is the record, not the rows. Restore is the current parser's
view of the transcripts, exactly as `analyze --all` is the current
extractors' view: lines that older parser versions stored as auxiliary
events and the current one skips stay in the backup verbatim but do not
become rows, duplicate rows collapse, and event ids may differ from the
originals. Nothing outside the store cites event ids.

## Not backed up

- The Chroma vector index and every derived table. They are rebuilt on
  restore.
- `file_snapshot` events whose session id is empty (a parser gap tracked
  separately); they belong to no session.
- The update-check cache, logs, and the ingest lock.

## Files on this machine

| Path | What |
|---|---|
| `~/.longhand/backup/` | the clone (`--path` moves it) |
| `~/.longhand/backup-state.json` | per-session marks from the last run, owner-readable only; never in the repository |
| `~/.longhand/backup-staging/` | transient, removed at the end of every run |
| `~/.longhand/logs/backup.log` | auto-mode outcomes and failures |

The `backup` section of `config.json` holds `enabled`, `remote`, `path`,
and `auto`. `longhand config --set backup.auto=true` is the same switch as
`backup auto on`; `enabled`, `remote`, and `path` are written by `init`.

## How git is called

Every git call is a list-form argv with `-c protocol.ext.allow=never`, and
`--` precedes every remote or path you supplied; nothing goes through a
shell. Credentials embedded in a URL (`https://user:token@host/…`) are masked
in every message Longhand prints. See SECURITY.md.
