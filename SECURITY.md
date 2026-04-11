# Security & Threat Model

Longhand is a local-first tool that ingests Claude Code session transcripts into a SQLite database and a ChromaDB vector store, both stored in `~/.longhand/`. This document describes its threat model, the trust boundaries, and the hardening measures in place.

## TL;DR

- **Local-only.** No network calls. Nothing leaves your machine. The only network activity is the one-time ChromaDB embedding model download (~80MB) and any commands you explicitly invoke (e.g. `git push`).
- **No subprocess execution.** Longhand never calls `subprocess`, `os.system`, `eval`, or `exec`. Zero command injection surface.
- **Parameterized SQL everywhere.** No SQL is built from f-strings. LIKE clauses escape `%` and `_` wildcards.
- **Bounded inputs.** Stdin readers, file sizes, line lengths, and filter strings are all capped to prevent DoS.
- **Read-only on the source data.** Longhand never writes back to `~/.claude/projects/` — it only reads JSONL files.
- **The hooks fail open.** If anything goes wrong inside a hook, it returns `{}` and Claude Code proceeds as if Longhand wasn't there.

If you find a hole, please open an issue or email me directly. I'd rather hear about it before it ships somewhere it shouldn't.

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────┐
│  Claude Code session                                    │
│  ├─ writes JSONL to ~/.claude/projects/<project>/*.jsonl│
│  ├─ fires SessionEnd hook → longhand ingest-session     │
│  └─ fires UserPromptSubmit hook → longhand __prompt-hook│
└─────────────────────────────────────────────────────────┘
                            │
                            ▼  (trust boundary)
┌─────────────────────────────────────────────────────────┐
│  Longhand (local Python process)                        │
│  ├─ reads JSONL files (read-only)                       │
│  ├─ writes to ~/.longhand/longhand.db (SQLite)          │
│  ├─ writes to ~/.longhand/chroma/ (ChromaDB)            │
│  └─ stdout: Rich CLI output OR hook JSON                │
└─────────────────────────────────────────────────────────┘
```

Anything outside `~/.longhand/` and `~/.claude/projects/` is out of scope. Longhand never touches the network, never modifies source files, and never executes shell commands derived from user input or stored data.

## Threat Model

### What Longhand defends against

| Threat                                  | Defense |
|-----------------------------------------|---------|
| Command injection via tool output       | No subprocess/eval/exec calls anywhere. Tool output is never executed. |
| SQL injection via search queries        | All SQL uses parameterized queries. LIKE wildcards escaped with `ESCAPE '\\'`. |
| Path traversal via file_path filters    | File paths from queries are used only as LIKE substrings against the indexed `events` table. Longhand never opens files based on user input — it only opens JSONL files inside `~/.claude/projects/`. |
| OOM via huge JSONL files                | Hard 500MB file size limit and 50MB per-line limit in `parser.py`. Lines exceeding the limit are skipped, not parsed. |
| OOM via huge prompts in the hook        | Stdin is bounded to 256KB. Prompts are truncated to 8000 chars before recall. |
| DoS via pathological LIKE patterns       | All keyword/path filters are length-capped (500 chars) and have `%`/`_`/`\\` escaped before use. |
| Hook crashing Claude Code               | The hook handler wraps everything in try/except and returns `{}` on any failure. Claude Code never sees an exception. |
| Malformed JSONL crashing the ingestor   | Lines that fail to parse as JSON are skipped, not crashed. The full parse continues. |
| Duplicate uuids across subagent streams | Detected and disambiguated with a counter suffix at parse time. |
| Embedding service exfiltration           | The default embedding model is ChromaDB's `all-MiniLM-L6-v2`, which runs locally via ONNX. No data is sent to OpenAI, Anthropic, or any other service. |

### What Longhand does NOT defend against

These are explicit non-goals — the threat is real but out of scope.

| Threat                                  | Why it's out of scope |
|-----------------------------------------|----------------------|
| **Local filesystem read access**        | If an attacker has read access to your home directory, they already have your `~/.claude/projects/` JSONL files, your SSH keys, your source code, your shell history, and everything else. Longhand storing the same data in `~/.longhand/` does not increase exposure. |
| **A malicious MCP client**              | The MCP server trusts the transport layer. If you let an untrusted MCP client connect to your local longhand stdio server, that client can read your indexed data. Don't do that. |
| **Sensitive content in your prompts**   | If you paste an API key into a Claude Code prompt, that key ends up in the JSONL file Claude Code writes. Longhand will index it because Longhand is forensic. The defense is "don't paste secrets into your prompts." |
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

Every keyword, file path, and project filter is truncated to 500 chars before use. The `_escape_like()` helper applies the truncation and escapes `%`, `_`, and `\`.

### Input bounds (mcp_server.py)

```python
MAX_LIMIT = 1000        # max result count for any MCP tool
MAX_OUTPUT_CHARS = 200000  # max output size for any MCP response
```

All MCP tool `limit` parameters are capped at 1000 via `_limit()`. All `max_chars` parameters are capped at 200KB via `_max_chars()`. Integer and boolean parameters are coerced from strings via `_int()`/`_bool()` to handle MCP bridge type mismatches.

### SQLite concurrency

```python
conn.execute("PRAGMA busy_timeout = 5000")
```

Every SQLite connection sets a 5-second busy timeout, preventing `SQLITE_BUSY` errors when the SessionEnd hook fires while a manual `longhand ingest` is running.

### Input bounds (setup_commands.py)

```python
_HOOK_STDIN_MAX_BYTES = 256 * 1024  # 256KB max stdin payload
_HOOK_PROMPT_MAX_LEN  = 8000        # max prompt length passed to recall
```

The UserPromptSubmit hook reads at most 256KB from stdin. The prompt is truncated to 8000 chars before being passed to the recall pipeline.

### File permissions

The `~/.longhand/` data directory is created with `mode=0o700` (owner-only read/write/execute). On shared systems, other users cannot read your session data, thinking blocks, or indexed content.

### Configurable injection

The `UserPromptSubmit` hook is tunable via `~/.longhand/config.json`:
- `hook.min_relevance` — minimum relevance score to inject context (default 2.5)
- `hook.max_inject_chars` — cap injection size to control token usage (default 2000 chars)
- `hook.enabled` — disable entirely without uninstalling

Users concerned about token costs or stale context injection can raise the threshold or cap the size.

### Fail-open hooks

All hook handlers wrap their full execution in try/except. On any exception they print `{}` to stdout and return cleanly. The intent is that Longhand can crash internally without ever crashing or hanging Claude Code.

### No subprocess, no eval

```bash
$ grep -rn 'subprocess\|os\.system\|os\.popen\|shell=True\|eval(\|exec(' longhand/
# (no results)
```

There are no subprocess calls, no shell invocations, no eval, and no exec anywhere in the Longhand source. The only external command Longhand can invoke is its own `mcp-server` subcommand via the Claude Desktop / Claude Code MCP integration, which is configured by the user explicitly.

### Parameterized SQL

Every `conn.execute()` call uses bound parameters. There are zero f-string SQL constructions. The only dynamic SQL is the placeholder list for `IN (?, ?, ?)` clauses, which uses fixed-string placeholders and bound parameters for the values.

### Read-only against source data

`parser.py` opens JSONL files with mode `"r"`. Longhand never writes back to `~/.claude/projects/`. The only directories Longhand writes to are:

- `~/.longhand/longhand.db` (SQLite)
- `~/.longhand/chroma/` (ChromaDB persistent collections)
- `~/.longhand/config.json` (only when you explicitly run `longhand config --set`)
- `~/.claude/settings.json` (only when you explicitly run `longhand hook install` or `longhand prompt-hook install`)
- `~/.claude/settings.json.longhand-backup` (created automatically before any settings.json modification)

## Reporting Issues

If you find a bug — security or otherwise — open an issue at https://github.com/Wynelson94/longhand/issues. For issues you don't want public, email me directly.

I'd rather hear about it.

## Out-of-scope Notes for Auditors

A few things that might look suspicious but aren't:

- **`__prompt-hook-run`** is the internal hook handler. It's prefixed with `__` and marked `hidden=True` in Typer so it doesn't show in `--help`. It only reads bounded stdin and only invokes the local recall pipeline. It never spawns subprocesses, never opens network sockets, and never reads files outside what the recall pipeline already accesses.
- **`__pycache__/` and `*.pyc`** are normal Python bytecode caches, not malicious files.
- **The `chromadb` dependency pulls in `onnxruntime`** for the local embedding model. ONNX runs in a sandboxed inference graph — it doesn't execute Python. The model itself (`all-MiniLM-L6-v2`) is open and well-known.
- **The `mcp` dependency is optional** (only required for the MCP server). The core CLI works without it.
- **`pip install -e .` is editable mode** — it's not a privilege escalation, it just installs the entry point so `longhand` works on your PATH.

If you're a security researcher and want to chat about the design, I'm happy to. The whole point of this tool is that the raw record never lies — and that includes the security model.

— Nate
