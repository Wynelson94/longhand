# Shared memory for Claude Code and Codex

Longhand keeps one archive for both clients. Claude Code sessions arrive
through the hooks you already have; Codex Desktop and Codex CLI threads (the
"rollouts" under `~/.codex/sessions`) arrive through `longhand codex-sync`.
Both land in `~/.longhand`, with Codex sessions namespaced `codex:<thread-id>`
so nothing collides. A question asked in either client can be answered from
work done in the other.

Requires Longhand 1.1.0 or newer.

## Set up in three commands

```sh
pip install -U longhand
longhand codex-sync        # capture every Codex thread on this machine
longhand doctor            # the "Codex capture" row confirms it
```

Then connect the shared keyword server to each client:

```sh
# Claude Code — alongside the existing `longhand` server
claude mcp add --scope user longhand-shared -- longhand shared-mcp

# Codex CLI
codex mcp add longhand -- longhand shared-mcp
```

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
`get_session_timeline`, and `get_event_text`. Search matches literal phrases,
not meaning. It reads the same SQLite rows both clients write, never opens
Chroma, and never loads a model; long texts and raw records page in
8,000-character slices. A broad query on a large archive can hit the server's
instruction budget — narrow it to a session.

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
--semantic` runs the full pipeline on everything immediately, and
`--no-finalize` keeps a run exact-only. `doctor` shows a "Codex finalizer" row
while any thread is archived; `analyze` never embeds events, so it is not the
remedy for one.

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
LaunchAgent that runs `codex-sync` at login and every 60 seconds. Fill in the
interpreter that has Longhand installed and your home directory, then load it:

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
codex-sync` with a systemd timer or cron. On Windows, `--watch` or Task
Scheduler.

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
  known shape.
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
