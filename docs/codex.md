# Shared memory for Claude Code and Codex

Longhand keeps one archive for both clients. Claude Code sessions arrive
through the hooks you already have; Codex Desktop and Codex CLI threads (the
"rollouts" under `~/.codex/sessions` and `~/.codex/archived_sessions` — or
wherever `CODEX_HOME`/`--codex-home` points) arrive through `longhand
codex-sync`. Both land in `~/.longhand`, with Codex sessions namespaced
`codex:<thread-id>` so nothing collides. A question asked in either client can
be answered from work done in the other.

Basic capture requires Longhand 1.1.0 or newer. The finalizer described below
(the 30-minute quiet rule, `--finalize-after`, `--no-finalize`, and doctor's
"Codex finalizer" row) needs 1.2.0+; before that, an active thread indexed
with `--semantic` regressed to exact-record-only the moment it grew, with
nothing to re-index it. Skipping `compacted` records (see *What is captured*
below) needs 1.2.2+.

## Set up in three commands

```sh
pip install -U longhand
longhand codex-sync        # capture Codex threads on this machine (up to 50/run; subagent threads skipped)
longhand doctor            # the "Codex capture" row confirms it
```

Then connect the shared keyword server to each client:

```sh
# Claude Code — alongside the `longhand` server, if you've registered one
claude mcp add --scope user longhand-shared -- longhand shared-mcp

# Codex CLI
codex mcp add longhand -- longhand shared-mcp
```

That "alongside" assumes you already have Claude Code's main `longhand` server registered — `longhand setup`/`mcp install` only wire up Claude *Desktop*. For Claude Code, add it explicitly: `claude mcp add longhand -s user -- longhand mcp-server` (or install the Claude Code plugin). The two servers are independent; `longhand-shared` works fine on its own.

Codex Desktop does not put `codex` on your PATH. Add the server to
`~/.codex/config.toml` instead and restart the app:

```toml
[mcp_servers.longhand]
command = "longhand"
args = ["shared-mcp"]

# Only if your archive is not at ~/.longhand:
[mcp_servers.longhand.env]
LONGHAND_DATA_DIR = "/Users/you/.longhand"
```

Use an absolute path to `longhand` (`which longhand`) when the desktop app's
PATH differs from your terminal's. If you relocated the archive with
`LONGHAND_DATA_DIR`, set the same value for both clients and for the capture
commands — one archive is the whole point.

## What each client sees

|                                                   | Claude Code `longhand` server | `longhand shared-mcp`      |
| ------------------------------------------------- | ----------------------------- | -------------------------- |
| Lists and pages Codex sessions                    | yes                           | yes                        |
| Keyword search across both clients                | no                            | yes (`search`, literal)    |
| `recall` and semantic search over Codex sessions  | 30 min after a thread quiets  | no                         |
| Commits made in Codex                             | yes (`find_commits`)          | through `search`           |
| Loads the embedding model                         | yes                           | never                      |

The shared server exposes four read-only tools: `list_sessions` (with
`source="codex"|"claude"` and `project` substring filters), `search`,
`get_session_timeline`, and `get_event_text`. Search matches literal phrases
(up to 200 characters), not meaning, and needs an exact `session_id` to scope
to one session — there's no prefix matching here, unlike the main `longhand`
server. `search` and `get_session_timeline` return 2,000-character excerpts
per event, each with a `total_chars` field so you know how much more there
is; every tool's `limit` is capped at 50. `get_session_timeline` orders by
`timestamp ASC, sequence ASC` rather than sequence alone (fixed in 1.2.1) —
a Claude Code subagent thread is stored under its parent `session_id` with
its own sequence restarting at 0, so ordering by sequence alone used to mix
rows from unrelated threads out of chronological order entirely. Timestamp
ordering fixes that, but a subagent that ran concurrently with its parent
still interleaves with it row-by-row in the output, chronologically — the
per-event `is_sidechain` field is what tells a subagent row from a parent
one, not physical separation. It reads the same SQLite rows both clients
write, never opens Chroma, and never loads a model; `get_event_text` pages
longer text or raw JSON in 8,000-character slices via its `offset` parameter.
A broad query on a large archive can hit the server's instruction budget —
narrow it to a session.

Capture runs in two passes, the Codex twin of Claude Code's Stop and SessionEnd
hooks. A new or changed rollout is first captured exact-record-only (ingestion
stage `archived`): every message, reasoning summary, tool call, and output is
stored verbatim and keyword-searchable, with no vector model loaded — the same
reason Claude's per-turn Stop hook skips embeddings. Codex sends no session-end
signal, so quiet stands in for it: once a rollout has been untouched for 30
minutes (`--finalize-after`, in seconds) it gets the full pipeline —
embeddings, episodes, project inference — and `recall` and semantic `search`
see it. A finalized thread that resumes is captured exact-only again and
finalized again once it settles: one re-embed per resume. `longhand codex-sync
--semantic` runs the full pipeline immediately on everything that scan
discovers (still bounded by `--limit`, default 50), and `--no-finalize` keeps
a run exact-only. `doctor` shows a "Codex finalizer" row while any thread is
archived; `analyze` never embeds events, so it is not the remedy for one. If
`analyze --all`/`--session` reaches an archived Codex thread anyway (it isn't
excluded from the session list `analyze` iterates), it stamps that thread
`analyzed` without ever calling the event-embedding step — and since the
finalizer only looks at rollouts *not already* `analyzed`, that thread is
then permanently skipped: not just deferred to the next scheduled run, but
invisible to `recall`/semantic `search` until the rollout file changes again
and triggers a fresh capture. The doctor "Codex finalizer" row goes quiet for
it too, since it also counts by the `archived` stage. Separately, a
long-running `longhand analyze --all` also holds the same ingest lock a
scheduled finalization pass needs, which can delay (not block) other threads'
finalization until the next scheduled run. Both effects are tracked in
[#97](https://github.com/Wynelson94/longhand/issues/97).

## Keeping capture current

`longhand reconcile --fix` runs both passes — captures new or changed Codex
rollouts and finalizes the quiet ones — along with everything it already does
for Claude transcripts, so the scheduled reconciler (`longhand schedule
install-reconciler`, every 30 minutes on macOS) keeps Codex current and
recallable with no extra setup. For an immediate run use `longhand codex-sync`;
for a foreground loop, `longhand codex-sync --watch` (every 60 seconds until
interrupted). On a 60-second schedule a thread is recallable about 30 minutes
after its last message; the poller loads the embedding model only on the run
that has a quiet thread to finalize, one thread per run.

### Faster capture with launchd (macOS)

`scripts/com.longhand.codex-sync.plist.template` is a ready-made user
LaunchAgent that runs `codex-sync` at login and every 60 seconds, capped at
`--limit 8` per run (the CLI's own default is 50 — edit the template if your
Codex usage regularly produces more new/changed rollouts a minute than that).
This file ships in the git repo, not in the `pip install longhand` wheel — if
you installed from PyPI, either fetch just this one file from GitHub or clone
the repo. Fill in the interpreter that has Longhand installed and your home
directory, then load it:

```sh
PY="$(command -v python3)"   # must be the Python that has longhand installed
mkdir -p ~/.longhand/logs
sed "s|__PYTHON__|$PY|g; s|__HOME__|$HOME|g" \
  scripts/com.longhand.codex-sync.plist.template \
  > ~/Library/LaunchAgents/com.longhand.codex-sync.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.longhand.codex-sync.plist
```

The job exits between scans, so `state = not running` with `last exit code = 0`
is normal. Inspect it and its capture reports with:

```sh
launchctl print "gui/$(id -u)/com.longhand.codex-sync"
tail -n 5 ~/.longhand/logs/codex-sync.log
```

launchd does not read your shell profile, so a relocated archive needs
`LONGHAND_DATA_DIR` in the plist's `EnvironmentVariables`. To stop capture
without deleting any archived history, `launchctl bootout "gui/$(id -u)"` the
plist and remove it.

On Linux, run `--watch` in a persistent terminal or schedule `longhand
codex-sync` with a systemd timer or cron. Windows isn't a supported platform
(see the README's Platform support section) — run Longhand under WSL2 and
follow the Linux guidance above.

## What is captured, and what is not

- **Threads you drove.** Codex also spawns threads for itself — its "guardian"
  approval reviewer, for one — that re-quote the parent thread. `codex-sync`
  skips them by default so every search hit appears once; `--include-subagents`
  captures them.
- **Canonical items only.** Codex writes every message twice: once as a
  canonical `response_item` and once as a UI `event_msg` mirror. The mirrors,
  token accounting, and turn bookkeeping are skipped so nothing is stored
  twice. Reasoning is stored when Codex provides a readable summary; encrypted
  reasoning has no readable content and is skipped.
- **Bounds.** Per run: up to 50 sessions, each up to 16 MiB and 20,000 events.
  Larger rollouts are reported as `deferred`, never partially imported. Raise
  the bounds with `--limit`, `--max-file-kb`, and `--max-events`. These are
  input bounds, not a memory ceiling — the exact-record pass never loads a
  model, and the finalizer loads it only when a quiet thread is waiting.
- **Drift is never silent.** A record shape Longhand does not recognize is
  preserved as an `unknown` event with its raw JSON intact, and surfaces in
  `longhand doctor`'s "Transcript format" row as `response_item/<kind>` or
  `event_msg/<kind>`. `tests/fixtures/codex_shapes/` regression-gates every
  known shape. Three record types are recognized and deliberately skipped
  rather than stored: `token_usage_record` and `world_state` (bookkeeping,
  since 1.1.0), and `compacted` (since 1.2.2) — Codex writes one when it
  compacts a thread's context window, and its `replacement_history` re-lists
  messages the rollout already carries. `doctor`'s drift row treats these
  skip types as understood even on rows stored *before* the skip existed, so
  upgrading doesn't produce a one-time false drift warning for data already
  on disk.
- **Shell commands are understood; scripts are text.** Commands run through
  Codex's shell tools, and the `cmd:` literals inside its `exec` scripts, feed
  error detection and git extraction, so commits made from Codex show up in
  `find_commits` and `longhand git-log`. Patches and the rest of a script stay
  recorded text — they are not translated into Claude-style file replay.
- **Redaction and locks apply.** Opt-in secret redaction covers Codex records.
  Capture runs under the same ingest lock as the hooks; a Claude hook that
  fires while a capture holds the lock skips that turn, and the reconciler
  heals it.

This is shared, retrievable history — not a transfer of a model's live
context. Only locally saved rollouts are available.

## Verify

```sh
python3 -m pytest tests/test_codex.py tests/test_codex_shapes.py -q
longhand codex-sync --dry-run
```

The tests use synthetic records and a sanitized fixture of real rollout
shapes; they cover cross-client storage and keyword retrieval, stable IDs,
redaction, subagent skipping, the skip rules, git extraction, bounded capture,
and reconcile's capture path.

Codex MCP configuration: [official documentation](https://learn.chatgpt.com/docs/extend/mcp).
