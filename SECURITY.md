# Security & Threat Model

Longhand is a local-first tool that ingests Claude Code session transcripts — and, since 1.1.0, Codex Desktop/CLI rollouts from `~/.codex/` — into a SQLite database and a ChromaDB vector store, both stored in `~/.longhand/` by default (relocatable with `LONGHAND_DATA_DIR` or `--data-dir`). This document describes its threat model, the trust boundaries, and the hardening measures in place.

## TL;DR

- **Local-only.** Nothing you stored ever leaves your machine. Network activity is limited to: the one-time ChromaDB embedding model download (~80MB), a version check against pypi.org (disable with `LONGHAND_NO_UPDATE_CHECK=1`; the request carries no data beyond the HTTP request itself), and commands you explicitly invoke (e.g. `git push`). The version check is excluded from Claude Code's hooks and from the `mcp-server` entry point that `mcp install` actually configures — but not yet from `longhand shared-mcp`, `mcp serve`, or `demo`, which run it at process exit like any other CLI command ([#101](https://github.com/Wynelson94/longhand/issues/101)); and `doctor` (so also `setup`, which calls it) forces a synchronous fetch on every run with a 2-second timeout rather than respecting the usual 24-hour cache.
- **No shell, no `eval`/`exec`.** Longhand never uses a shell, `os.system`, `os.popen`, `eval`, or `exec`. It does spawn fixed, list-form subprocesses — `launchctl` (to (un)load the optional reconciler) and detached background workers (`[sys.executable, "-m", "longhand", "ingest"]` / `[..., "backfill-episodes"]`) — none of which interpolates user input or stored data, so the command-injection surface is zero.
- **Parameterized SQL everywhere.** Every user-supplied value reaches SQLite as a bound parameter, never interpolated into SQL. LIKE clauses escape `%`, `_`, and `\`. Several f-strings build SQL text, but only from fixed, internal strings — never user input: the `?` placeholder list for `IN (?, ?, ?)` clauses, `PRAGMA table_info({table})` with a hardcoded table name, the `redact` command's column/table names (from a fixed internal dict), `get_events`' `ORDER BY {direction}` (one of two literal strings, `"ASC"`/`"DESC"`), and the shared server's `lightweight_mcp.py` (a `WHERE` clause built from fixed fragments, and `substr({column}, …)` where `column` is one of two literal names picked by a boolean). This list has grown every time it's been re-audited — the guarantee is that every addition has been a fixed internal string, never a value read from a query, not that this is the complete count.
- **Bounded inputs.** Stdin readers, file sizes, line lengths, and filter strings are all capped to prevent DoS.
- **Read-only on the source data.** Longhand never writes back to `~/.claude/projects/` or `~/.codex/` — it only reads JSONL/rollout files, whether from the hooks, `codex-sync`, or an explicit `ingest <PATH>` / `--transcript` argument.
- **The hooks fail open, but not identically.** All three exit 0 on failure. `UserPromptSubmit` prints `{}`. `SessionEnd` (`ingest-session`) additionally writes a one-line breadcrumb to `logs/hook-errors-YYYY-MM-DD.log`, surfaced by `doctor`. `Stop` (`ingest-live`) fails silently — no output, no breadcrumb — by design, since it's the one hook that runs on every turn.

If you find a hole, please open an issue or email me directly. I'd rather hear about it before it ships somewhere it shouldn't.

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code session              Codex Desktop / CLI    │
│  ├─ writes JSONL to               ├─ writes rollouts to  │
│  │  ~/.claude/projects/<p>/*.jsonl│  ~/.codex/(archived_)sessions │
│  ├─ fires SessionEnd → ingest-session                    │
│  ├─ fires Stop (per turn) → ingest-live (live tail)      │
│  ├─ fires UserPromptSubmit → __prompt-hook-run           │
│  └─                                └─ polled by codex-sync / reconcile │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼  (trust boundary)
┌─────────────────────────────────────────────────────────┐
│  Longhand (local Python process)                        │
│  ├─ reads JSONL/rollout files (read-only)               │
│  ├─ writes to ~/.longhand/longhand.db (SQLite)          │
│  ├─ writes to ~/.longhand/chroma/ (ChromaDB)             │
│  ├─ writes logs/, cache/, .ingest.lock, update-check.json│
│  └─ stdout: Rich CLI output, hook JSON, or MCP responses │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼  (second trust boundary, opt-in)
┌─────────────────────────────────────────────────────────┐
│  `longhand shared-mcp` (a.k.a. longhand-shared)          │
│  Keyword-only read access to the WHOLE archive above —   │
│  Claude transcripts included — for whatever second       │
│  client/model provider it's registered with (e.g. Codex) │
└─────────────────────────────────────────────────────────┘
```

Anything outside `~/.longhand/`, `~/.claude/projects/`, and `~/.codex/` is out of scope. Longhand never modifies source files and never executes shell commands derived from user input or stored data. It is not otherwise silent on the network, though — see the version-check and embedding-model bullets above — but no LLM or cloud service ever receives your data: the update check carries only its own HTTP request, and the ONNX model fetch is a fixed download, never an upload.

**The shared-server boundary is worth calling out on its own.** `longhand shared-mcp` (registered as `longhand-shared` in Claude Code, `longhand` in Codex) is read-only, but it reads the *entire* archive — Claude Code transcripts and thinking blocks included — over SQLite in `mode=ro`, with no per-client partitioning. Registering it with a second AI client or model provider (exactly what the Codex integration does) gives that provider keyword search over everything Longhand has indexed, and `get_event_text(raw=true)` returns the stored raw JSON verbatim. That's the intended design — one shared archive for both clients — but it means the boundary for "does my Claude Code history reach a second model provider" is drawn at whether you run that command, not somewhere else.

## Threat Model

### What Longhand defends against

| Threat                                  | Defense |
|-----------------------------------------|---------|
| Command injection via tool output       | No shell, `eval`, or `exec`. The two `subprocess` spawns use fixed, list-form argv (`launchctl …`, `[sys.executable, "-m", "longhand", "ingest"\|"backfill-episodes"]`) with no user- or tool-derived arguments. Tool output is never executed. |
| SQL injection via search queries        | All SQL uses parameterized queries. LIKE wildcards escaped with `ESCAPE '\\'`. |
| Path traversal via file_path filters    | `search`'s `file_path_contains` filter is a case-insensitive Python substring check on each vector hit's `file_path` metadata, applied after the Chroma query (no SQL involved) and never used to open a file; `get_file_history`'s `file_path` is an exact match (`WHERE file_path = ?`), not a substring search, and neither opens a file either way. Longhand does open files at paths you give it directly — `ingest <PATH>`, `--transcript`, the hooks' stdin `transcript_path`, and rollouts under `~/.codex/` — but that's an explicit ingest target, not a query-driven read. |
| OOM via huge JSONL files                | Hard 500MB file size limit and 50MB per-line limit in `parser.py`. Lines exceeding the limit are skipped, not parsed. |
| OOM via huge prompts in the hook        | Stdin is bounded to 256KB. Prompts are truncated to 8000 chars before recall. |
| DoS via pathological LIKE patterns       | SQL-backed keyword/path filters are length-capped (500 chars) and have `%`/`_`/`\\` escaped before use (the shared server's `project` filter is escaped but not yet length-capped; `search`'s `file_path_contains` never reaches SQL — it's a Python substring check on vector hits — so it's neither capped nor escaped). |
| Hook crashing Claude Code               | Every hook handler wraps its full execution in try/except and exits 0 on any failure — Claude Code never sees an exception. `UserPromptSubmit` additionally prints `{}` so its output is always valid JSON. |
| Malformed JSONL crashing the ingestor   | Lines that fail to parse as JSON are skipped, not crashed. The full parse continues. |
| Duplicate uuids across subagent streams | Detected and disambiguated with a counter suffix at parse time. |
| Embedding service exfiltration           | The default embedding model is ChromaDB's `all-MiniLM-L6-v2`, which runs locally via ONNX. No data is sent to OpenAI, Anthropic, or any other service. |

### What Longhand does NOT defend against

These are explicit non-goals — the threat is real but out of scope.

| Threat                                  | Why it's out of scope |
|-----------------------------------------|----------------------|
| **Local filesystem read access**        | If an attacker has read access to your home directory, they already have your `~/.claude/projects/` JSONL files, your SSH keys, your source code, your shell history, and everything else. Longhand storing the same data in `~/.longhand/` does not increase exposure. |
| **A malicious MCP client**              | The MCP server trusts the transport layer. If you let an untrusted MCP client connect to your local longhand stdio server, that client can read your indexed data. Don't do that. |
| **Sensitive content in your prompts**   | If you paste an API key into a Claude Code prompt, that key ends up in the JSONL file Claude Code writes. Longhand is forensic by default and will index it. Mitigation: enable opt-in redaction (`longhand config --set redact.enabled=true`) to mask secret-shaped strings (AWS/GitHub/Anthropic/OpenAI/Slack/Stripe keys, JWTs, DB URLs with passwords, SSNs, plausible card numbers) at ingest, and run `longhand redact --apply` to retroactively mask data ingested earlier. Detection is pattern-based, not exhaustive — an arbitrary high-entropy string is not caught, so "don't paste secrets" remains the first line of defense. Git commit messages extracted into `git_operations.commit_message` are a known gap in the at-ingest path — `redact --apply` does cover that column retroactively, but `redact.enabled=true` doesn't yet catch a secret-bearing commit message the moment it's captured ([#110](https://github.com/Wynelson94/longhand/issues/110)). |
| **A malicious Claude Code session**     | If Claude Code itself were compromised and wrote malicious JSONL with a 5GB single line, the parser would skip that line (due to MAX_LINE_LENGTH). It would not crash, but Longhand assumes the JSONL files in `~/.claude/projects/` were written by a legitimate Claude Code instance. |
| **Disk encryption / at-rest security**  | Longhand stores data in plain SQLite and ChromaDB. If you need at-rest encryption, encrypt your filesystem (FileVault on macOS, LUKS on Linux). |

## Hardening Measures

### Input bounds (parser.py)

```python
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500MB per session file
MAX_LINE_LENGTH     = 50  * 1024 * 1024  # 50MB per JSONL line
```

A session file larger than 500MB raises immediately. A single line larger than 50MB is skipped, allowing the rest of the file to parse. Both limits exist to prevent OOM from malformed or malicious JSONL.

### Input bounds (storage/sqlite_store.py)

```python
MAX_FILTER_LENGTH = 500  # max length for any user-provided keyword/path filter
```

Every keyword, file path, and project filter that reaches SQL is truncated to 500 chars before use (the exceptions are in the threat table above). The `_escape_like()` helper applies the truncation and escapes `%`, `_`, and `\`.

### Input bounds (mcp_server.py)

```python
MAX_LIMIT = 1000        # max result count for any MCP tool
MAX_OUTPUT_CHARS = 200000  # max output size for any MCP response
```

All MCP tool `limit` parameters are capped at 1000 via `_limit()`. `max_chars` parameters are clamped to 200KB at the top end via `_max_chars()` — but `0` or a negative value disables truncation entirely, and 6 of the 13 tools (`get_file_history`, `replay_file`, `get_stats`, `find_episodes`, `list_plans`, `reconcile`) never call `_max_chars`/`_truncate_output` at all — and neither do `list_sessions(project_id=…)` or `list_projects(match=…)` — so their output is unbounded by this mechanism regardless ([#103](https://github.com/Wynelson94/longhand/issues/103)). Integer and boolean parameters are coerced from strings via `_int()`/`_bool()` to handle MCP bridge type mismatches.

### SQLite concurrency

```python
conn.execute("PRAGMA busy_timeout = 30000")
```

Every connection opened through `SQLiteStore.connect()` sets a 30-second busy timeout, preventing `SQLITE_BUSY` errors when the SessionEnd hook fires while a manual `longhand ingest` is running. Two call sites open their own raw `sqlite3.connect()` outside that helper and don't set it: `db vacuum`'s VACUUM connection (which already refuses to run alongside a concurrent ingest via its own lock check) and the read-only connection `longhand shared-mcp` opens per call.

### Input bounds (setup_commands.py)

```python
_HOOK_STDIN_MAX_BYTES = 256 * 1024  # 256KB max stdin payload
_HOOK_PROMPT_MAX_LEN  = 8000        # max prompt length passed to recall
```

The UserPromptSubmit hook reads at most 256KB from stdin. The prompt is truncated to 8000 chars before being passed to the recall pipeline.

### File permissions

`LonghandStore.__init__` creates `~/.longhand/` with `mode=0o700` (owner-only read/write/execute) on every store open, which covers the CLI, hooks, and MCP server. Several narrower paths don't apply that mode as reliably ([#99](https://github.com/Wynelson94/longhand/issues/99)): the update-check cache writer and `longhand config --set` both `mkdir(parents=True, exist_ok=True)` with no explicit mode at all; and `schedule install-reconciler` and the hook-error logger both do pass `mode=0o700`, but to `mkdir(parents=True, exist_ok=True)` on a `logs/` *subdirectory* — Python's `pathlib.Path.mkdir(parents=True, ...)` applies `mode` only to the leaf directory it creates, not to any missing parents, so if `~/.longhand` itself doesn't exist yet, that parent is created with the process's default permissions and only `logs/` ends up 0700. On shared systems this only matters if one of these paths runs before anything else has created `~/.longhand`.

### Configurable injection

The `UserPromptSubmit` hook is tunable via `~/.longhand/config.json`:
- `hook.min_relevance` — minimum relevance score to inject context (default 2.5)
- `hook.max_inject_chars` — cap injection size to control token usage (default 2000 chars)
- `hook.max_episodes` — max episodes considered per query (default 2)
- `hook.enabled` — disable entirely without uninstalling

Users concerned about token costs or stale context injection can raise the threshold or cap the size.

### Fail-open hooks, per hook

All hook handlers wrap their full execution in try/except and exit 0 on any failure — Longhand can crash internally without ever crashing or hanging Claude Code. What happens beyond the exit code differs by hook: `UserPromptSubmit` prints `{}` to stdout; `SessionEnd` prints one line to stderr and appends a breadcrumb to `logs/hook-errors-YYYY-MM-DD.log` (surfaced by `doctor`); `Stop` (the per-turn live tail) does neither — it returns quietly with no output and no breadcrumb, since it must never add I/O to the path that runs on every assistant turn.

### Subprocess use — no shell, no injection

Longhand uses `subprocess` in exactly two places. Both pass a fixed, list-form argv (never a shell string), and neither includes any value derived from user input, tool output, or stored data:

- `longhand/setup_commands.py` — `["launchctl", "unload"|"load", RECONCILER_PLIST_PATH]`, run only when you explicitly install or uninstall the optional reconciler. The plist path is a fixed module constant.
- `longhand/recall/project_fallback.py` — `subprocess.Popen([sys.executable, "-m", "longhand", "ingest"], close_fds=True, start_new_session=True)`, a detached background re-ingest the recall pipeline may spawn when it notices a stale index (the same `spawn_background` helper also spawns `[..., "backfill-episodes"]` on the analogous episode-backfill path). `-m longhand.cli` was the actual entry point through 0.11.1; it was a bug (fixed in 0.11.2), not the current behavior.

There is no `shell=True`, no `os.system`/`os.popen`, and no `eval`/`exec` anywhere in the source — verify with:

```bash
$ grep -rn 'shell=True\|os\.system\|os\.popen\|eval(\|exec(' longhand/
# (no results)
```

Because every argv element is a string literal or an internal constant, there is no command-injection surface even though `subprocess` itself is used.

### Parameterized SQL

Every user-supplied value reaches SQLite as a bound parameter — no user input is ever interpolated into SQL; only fixed, internal strings ever go into an f-string that builds SQL text. Beyond the `IN (?, ?, ?)` placeholder list (fixed `?` strings, bound values) and `PRAGMA table_info({table})` in `migrations.py` (`{table}` is a hardcoded internal name from a fixed dict), this also covers: the `redact` command's per-table column/PK names, sourced from the fixed `_REDACT_TABLES` dict in `cli/_commands.py`; `get_events`' `ORDER BY timestamp {direction}, sequence {direction}` in `sqlite_store.py`, where `{direction}` is one of exactly two literals (`"ASC"`/`"DESC"`) chosen by a boolean flag, never passed through from a caller; and `lightweight_mcp.py`'s `list_sessions`/`search` (a `WHERE` clause assembled by joining fixed condition fragments) and `get_event_text` (`substr({column}, …)` where `column` is `"raw_json"` or `"content"`, chosen by a boolean). New instances of this pattern have turned up each time this file has been re-audited; the invariant that holds across all of them is that the interpolated piece is always a fixed internal string, never user input.

### Read-only against source data

`parser.py` and `codex.py` open transcript/rollout files with mode `"r"`. Longhand never writes back to `~/.claude/projects/` or `~/.codex/`. The paths Longhand does write to, beyond the SQLite and Chroma stores already named at the top of this document:

- `~/.longhand/longhand.db` (SQLite) and `~/.longhand/chroma/` (ChromaDB persistent collections)
- `~/.longhand/config.json` (only when you explicitly run `longhand config --set` — this one path is written even with `LONGHAND_DATA_DIR`/`--data-dir` set to something else, [#96](https://github.com/Wynelson94/longhand/issues/96))
- `~/.longhand/update-check.json` (the version-check cache; refreshed roughly once a day per most commands, forced on every `doctor`/`setup` run — see the network bullet above)
- `~/.longhand/.ingest.lock` (held for the duration of an ingest/analyze/vacuum/reattribute run)
- `~/.longhand/cache/jsonl_project_map.json` (drift-detection cache)
- `~/.longhand/logs/` — `hook-errors-YYYY-MM-DD.log` (SessionEnd hook failures), `background-ingest-YYYY-MM-DD.log` / `background-backfill-YYYY-MM-DD.log` (the two detached workers, named by their `log_prefix`), `reconcile.log` (the scheduled reconciler), `codex-sync.log` (the launchd Codex poller)
- `~/Library/LaunchAgents/com.longhand.reconcile.plist` (only when you run `schedule install-reconciler`, macOS only) and the equivalent `com.longhand.codex-sync.plist` if you install the template from `docs/codex.md`
- `~/Library/Application Support/Claude/claude_desktop_config.json` and its `.longhand-backup` (only when you run `longhand mcp install`/`mcp uninstall`, or `setup` without `--skip-mcp`)
- `~/.claude/settings.json` (when you run `longhand hook install` or `prompt-hook install` directly; `setup` always writes the SessionEnd/Stop hooks in step 2 — there's no flag to skip those — and writes the prompt hook too unless you pass `--skip-prompt-hook`; the corresponding `uninstall` commands write it as well) and `~/.claude/settings.json.longhand-backup` (created automatically before any `settings.json` modification)
- `~/.cache/chroma/onnx_models/` — chromadb's own cache directory for the downloaded embedding model, outside `~/.longhand/` entirely
- `longhand-demo-<timestamp>/` under the OS temp directory (`tempfile.gettempdir()` — `/tmp/` on Linux, typically `/var/folders/…/T/` on macOS, not literally `/tmp/`) — the sandboxed store `longhand demo` creates and (by default) cleans up; `--keep` leaves it on disk

## Reporting Issues

If you find a bug — security or otherwise — open an issue at https://github.com/Wynelson94/longhand/issues. For issues you don't want public, email me directly.

I'd rather hear about it.

## Out-of-scope Notes for Auditors

A few things that might look suspicious but aren't:

- **`__prompt-hook-run`** is the internal hook handler. It's prefixed with `__` and marked `hidden=True` in Typer so it doesn't show in `--help`. It only reads bounded stdin and only invokes the local recall pipeline. That pipeline may spawn one detached, fixed-argv `python -m longhand ingest` to refresh a stale index (see *Subprocess use — no shell, no injection* above); it never opens network sockets and never reads files outside what the recall pipeline already accesses.
- **`__pycache__/` and `*.pyc`** are normal Python bytecode caches, not malicious files.
- **The `chromadb` dependency pulls in `onnxruntime`** for the local embedding model. ONNX runs in a sandboxed inference graph — it doesn't execute Python. The model itself (`all-MiniLM-L6-v2`) is open and well-known.
- **The `mcp` dependency is required** because the MCP server ships in the main package. It's a well-known Python client/server library published by Anthropic; see the [mcp package on PyPI](https://pypi.org/project/mcp/).
- **`pip install longhand`** installs the CLI and entry point via PyPI. The editable variant (`pip install -e .`) is only used for development — it's not a privilege escalation, it just installs the entry point so `longhand` works on your PATH.

If you're a security researcher and want to chat about the design, I'm happy to. The whole point of this tool is that the raw record never lies — and that includes the security model.

— Nate
