<!-- mcp-name: io.github.Wynelson94/longhand -->

# Longhand

[![Longhand MCP server](https://glama.ai/mcp/servers/Wynelson94/longhand/badges/score.svg)](https://glama.ai/mcp/servers/Wynelson94/longhand)
[![PyPI version](https://img.shields.io/pypi/v/longhand?label=PyPI&color=blue)](https://pypi.org/project/longhand/)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Tests](https://img.shields.io/badge/tests-174%20passing-brightgreen)
![Local](https://img.shields.io/badge/100%25-local-informational)

**Persistent local memory for Claude Code.** Every tool call, every file edit, every thinking block from every Claude Code session — stored verbatim on your machine. Searchable, replayable, and recallable by fuzzy natural-language questions. Zero API calls. Zero summaries. Zero decisions made by an AI about what's worth remembering.

> **Claude Code quietly rotates your session files after a few weeks.** Longhand captures them into SQLite before they're gone. Once ingested, your history stays forever — even after the source JSONL files are deleted. Install early; the past you don't capture is unrecoverable.

> **If you have 20+ Claude Code sessions in `~/.claude/projects/`, Longhand can find any fix, decision, or conversation you've had in ~126ms — without a single API call.**

> **Does it use a lot of tokens? No — every tool is capped by design.** A full `recall` across 100+ sessions returns ~4K tokens. Reading one raw session JSONL costs 10–50× more. See [Token budget](#token-budget).

```bash
pip install longhand
longhand setup        # ingest history + install hooks + configure MCP
longhand recall "that stripe webhook bug from last week"
```

**Upgrading from 0.5.x?** v0.6.0 improves how sessions get attributed to projects — multi-project sessions (ones that `cd` between repos during one Claude Code run) now attribute to the project where most of the work happened instead of the first-event cwd. Existing data benefits from a one-time pass:

```bash
pip install --upgrade longhand
longhand reconcile --fix   # re-attribute existing sessions and catch any the hook missed
```

Or, if you're upgrading from 0.5.8 or earlier (pre-improved episode summaries), chain them: `longhand reconcile --fix && longhand reanalyze`. Both are idempotent.

**Large history? (>1 GB of `~/.claude/projects`)** Expect the first-time backfill to take 10–30 minutes on an M-class Mac — most of that wall time is the embedding model running on all your cores (which is why you'll see triple-digit CPU%; that's ONNX doing its job, not a hang). To get a working store faster, use the fast-path:

```bash
longhand setup --skip-analysis   # SQLite only; works in ~1 min for multi-GB corpora
longhand reanalyze               # fill in episodes + vectors whenever, safe to background
```

Exact-text search, timelines, file history, and commit lookup all work after `--skip-analysis`. Semantic `recall` needs the `reanalyze` pass to complete. Typical throughput on an M-class Mac is ~1–2 sessions/sec for full analysis.

> *Status: v0.6.0 — stable, daily-driver tested, security-audited (zero critical findings), on PyPI, available as a Claude Code plugin. Validated against 131+ real Claude Code sessions across 40+ inferred projects. 182 unit tests passing.*

**Full docs:** [Longhand Wiki](https://github.com/Wynelson94/longhand/wiki) — getting started, CLI reference, MCP tools reference, architecture, and troubleshooting.

![Longhand demo](demo/longhand-demo.gif)

---

## The Inversion

Everyone is solving AI memory by making the context window bigger. 1M tokens. 2M tokens. Context-infinite. The whole industry is racing in the same direction: make the model carry more state.

Longhand goes the other direction. **The model doesn't need to carry the memory. The disk does.**

|                      | Bigger context windows                       | Longhand                          |
|----------------------|----------------------------------------------|-----------------------------------|
| **Where it lives**   | Rented from a model provider                 | A SQLite file + ChromaDB on your laptop |
| **Cost per query**   | Tokens × dollars                             | Zero                              |
| **Privacy**          | Goes through someone else's servers          | Never leaves your machine         |
| **Speed**            | Seconds to minutes for large contexts        | ~126ms                            |
| **Loss**             | Attention degrades in the middle of long contexts | Every event from the source file, nothing dropped |
| **Persistence**      | Dies when the window closes                  | Lives until you delete the file   |
| **Across model versions** | Doesn't transfer                        | Same data, any model              |
| **Offline**          | No                                            | Yes                               |
| **Scales with**      | Provider's pricing                           | Your hard drive                   |

The "memory crisis" in AI was an artificial constraint. Storage is solved. SQLite is from 2000. ChromaDB is two years old. Both run on a laptop. Longhand bypasses the crisis by ignoring it — your past sessions are already on disk, written by Claude Code itself, in JSONL files that contain every single event verbatim. Longhand reads those files, indexes them locally, and gives you semantic recall over your entire history without ever sending a token through someone else's API.

**Local. Complete. Yours.**

> **Storage footprint:** ~1GB for a heavy power user (120+ sessions, 60k events, months of daily Opus usage across 14 repos). Typical users: 200–400MB. Once Claude Code rotates the source files off disk, Longhand isn't a duplicate — it's the only copy.

---

## Python version note

Python 3.10 – 3.13 are fully supported. **On Python 3.14**, longhand pins `chromadb<1.0` automatically because chromadb's newer Rust bindings segfault on 3.14 (see [#4](https://github.com/Wynelson94/longhand/issues/4)). Once chromadb ships a 3.14-compatible 1.x wheel, the constraint will relax.

---

## Longhand vs claude-mem

[`thedotmack/claude-mem`](https://github.com/thedotmack/claude-mem) is the most popular Claude Code memory tool on GitHub (55k+ stars). It's a good tool. It is also solving the memory problem in the opposite direction from Longhand, and the difference is worth understanding before you pick one.

|                            | claude-mem                                   | Longhand                                     |
|----------------------------|----------------------------------------------|----------------------------------------------|
| **What's stored**          | AI-generated summaries / "observations"      | Verbatim events from the raw JSONL           |
| **Who decides what's kept**| An LLM, at write time                        | Nobody — everything is kept                  |
| **Compression**            | Semantic (lossy, by design)                  | None (lossless)                              |
| **API calls per session**  | One or more (calls Claude to summarize)      | Zero                                         |
| **Thinking blocks**        | Typically folded into summaries              | First-class, stored verbatim                 |
| **Deterministic replay**   | No — summaries can't reconstruct file state  | Yes — every diff kept and replayable         |
| **Model portability**      | Tied to the summarizer's output              | Same data works across any model, forever   |
| **Runtime**                | TypeScript, Bun, HTTP worker on :37777       | Python, no server                            |
| **License**                | AGPL-3.0                                     | MIT                                          |

The philosophical split: **claude-mem asks an AI what was important and keeps that. Longhand keeps the actual bytes and lets you decide later.** If you trust a model's judgment about its own past, claude-mem's approach is cheaper at query time (pre-summarized) and easier on storage. If you've ever been burned by a summary that dropped the thing that turned out to matter, Longhand is the tool that never throws anything away.

Both can coexist on the same machine — they operate on the same JSONL files without interfering.

---

## The Principles

Longhand is built on a handful of principles. If you disagree with them, you probably want a different tool.

### 1. Information doesn't disappear — it moves.

When data goes "missing" it's almost never actually gone. It got compressed, summarized, filed somewhere else, or renamed. Find the raw source and the truth is still there waiting. Claude Code already writes every session to disk as JSONL. That file is the raw source. Longhand just reads it.

### 2. Summarization is a lossy decision disguised as a convenience.

Most AI memory systems read a conversation and ask the AI to write down "what mattered." The AI is now the gatekeeper of its own memory, and the AI has incentives — brevity, confidence, coherence — that aren't the same as truth. You end up with a story about what happened instead of what happened.

Longhand never summarizes. It stores the complete record and lets you query it.

### 3. The raw record is cheap. Acting like it isn't wastes it.

A full Claude Code JSONL file is kilobytes to low megabytes. A year of daily sessions is hundreds of megabytes. That is nothing on modern hardware. There is no engineering reason to throw the data away. Summary-based memory isn't saving space — it's giving away information that was free.

### 4. The thinking is the most valuable part.

When Claude produces a `thinking` block, that's the reasoning behind the decision — usually invisible to the user, almost always more useful than the final answer. Summary-based memory throws thinking blocks away because they're "internal." Longhand treats them as first-class events. "What was I thinking when I chose to use a conditional update?" pulls the verbatim thinking block that contains the answer.

### 5. A fix you can't reproduce is a fix you didn't keep.

If you fixed a bug in March, the state of that file when the bug was fixed is a fact. Longhand reconstructs it deterministically by applying every edit in sequence from the session JSONL. No guessing, no AI inference, just literal application of the diffs. You can see the exact state of any file at any point in any past session.

### 6. Memory should be proactive, not just searchable.

A searchable archive is useful but passive. Real memory answers fuzzy questions. "A couple months ago I was building a game that kept breaking, then you fixed it — bring that fix forward." Longhand parses the time phrase, matches the project, finds the problem→fix episode, and returns the diff. You don't have to know the session ID. You just have to remember that it happened.

### 7. Deterministic beats clever.

Everything in Longhand's analysis is rules-based. Regex error detection. Hash-based project IDs. Forward-walking episode extraction. No LLMs in the core pipeline. That means fast (< 200ms recall queries), reproducible (same input → same output), and fully local (no API keys, no cloud). An LLM layer could go on top later, but the foundation runs on laws, not on a model's opinion.

### 8. Local or nothing.

Your Claude Code history is yours. It goes into a SQLite file and a ChromaDB directory in `~/.longhand/`. No telemetry. No sync. No account. If your laptop is offline, Longhand works. If Anthropic goes down, Longhand works. If you delete the directory, it's gone.

---

## What It Actually Does

When you use Claude Code, every session writes a JSONL file to `~/.claude/projects/<project>/<session-id>.jsonl`. That file contains every message, every tool call, every thinking block, every file edit with full before/after content, and a millisecond-precise timestamp for each event.

Longhand reads those files. Then it gives you:

- **Semantic search** across every event you've ever generated
- **Filterable search** — by tool, file, session, project, time range, event type — all filters combinable
- **Tool call archaeology** — "show me every Bash command I ran in March that touched Supabase"
- **File history across sessions** — every edit to a specific file, chronologically, across all your sessions
- **Session replay** — reconstruct any file's state at any point in any past session
- **Reasoning retrieval** — query Claude's verbatim thinking blocks
- **Timeline view** — chronological playback with pagination (offset, tail, summary-only scan mode)
- **Fuzzy recall** — natural-language questions about past work ("that race condition fix from last week")
- **Project inference** — automatic detection of which projects you've worked on, with categories and aliases
- **Episode extraction** — automatic detection of problem→fix sequences in your sessions
- **Conversation segments** — topic-level clustering (stories, design discussions, debugging, planning) so recall finds the *why*, not just the *what*
- **Git-aware project recall** — ask "where did we leave off on X" and get recent commits, unresolved issues, last session outcome in one call
- **Git commit extraction** — structured extraction of every git commit, push, merge, checkout from sessions, linked to episodes
- **MCP server** — 16 tools that let Claude query Longhand directly during live conversations
- **Auto-ingest hook** — drops into Claude Code's `SessionEnd` hook so new sessions are indexed automatically
- **Context injection** — `UserPromptSubmit` hook auto-injects relevant past context before Claude sees your message (configurable threshold and size cap)
- **Configurable** — `longhand config` to tune injection relevance, token budget, and behavior without editing code

---

## Install

```bash
pip install longhand
longhand setup
```

That's it. `longhand setup` backfills your existing Claude Code history, installs the hooks that keep it updated automatically, registers Longhand as an MCP server for Claude Code, and verifies everything works. About two minutes the first time, zero maintenance after that.

To upgrade later: `pip install -U longhand`.

### Developer install (from source)

```bash
git clone https://github.com/Wynelson94/longhand.git
cd longhand
pip install -e .
longhand setup
```

<details>
<summary>Or run the individual commands yourself</summary>

```bash
longhand ingest                # ingest all your existing Claude Code history
longhand analyze --all         # run analysis (projects, outcomes, episodes, segments)
longhand hook install          # auto-ingest every future session
longhand prompt-hook install   # (optional) auto-inject past context into new prompts
longhand mcp install           # let Claude Code call Longhand as MCP tools
longhand config                # view/tune hook behavior (relevance threshold, injection size)
longhand doctor                # verify everything is wired up
```
</details>

---

## Quick Start

```bash
# What's in the archive?
longhand stats
longhand sessions
longhand projects

# Daily-use commands
longhand recap                              # what have I been up to
longhand recap --days 30 --project bsoi     # filtered recap
longhand continue <session-id>              # pick up where I left off (session-scoped)
longhand status <project-name>              # where did we leave off on a project (git-aware)
longhand patterns                           # what bugs do I keep fixing
longhand history src/app/route.ts           # every edit ever to a file

# Semantic search
longhand search "race condition"
longhand search "stripe webhook" --tool Edit
longhand search "why did we" --type assistant_thinking

# Proactive recall (the fun one)
longhand recall "that clerk type error I fixed a couple weeks ago"
longhand recall "the python missing module bug last month"

# Session inspection
longhand timeline <session-id-prefix>
longhand replay <session-id> /path/to/file.ts
longhand diff <event-id>

# Git history
longhand git-log                            # recent git operations across all sessions
longhand git-log <session-id>               # git ops in a specific session
longhand git-log --type commit              # only commits
longhand git-log --query "fix parser"       # search commit messages

# Export
longhand export latest-fix                  # most recent resolved episode
longhand export ep_<id> --out fix.md        # specific episode to file
longhand export <session-id-prefix>         # full session timeline

# Configuration
longhand config                             # show current hook settings
longhand config --set hook.min_relevance=3.0  # tune injection threshold
longhand config --set hook.max_inject_chars=1000  # cap token usage
```

Session IDs accept prefix matches — `longhand timeline cf86` is enough if only one session starts with that.

---

## Recall Example

```
$ longhand recall "that stripe webhook I was fixing"

╭─ Project matches ───────────────────────────────────────╮
│ new-product (nextjs web app) · alias: 'stripe' · 1.52   │
╰─────────────────────────────────────────────────────────╯

Found it: new-product · 2 weeks ago · session a4ba29d1

### What went wrong
Type error: Property 'current_period_end' does not exist on type 'Subscription'.

### How it was diagnosed
```
In Stripe's type definitions, current_period_end moved off the Subscription
interface. It's still on the actual API payload but the types don't expose it.
We need to cast through Record<string, unknown> to access it.
```

### The fix
Edit on route.ts: 'const periodEnd = sub.current_period_end' → 'const periodEnd
= (sub as Stripe.Subscription & Record<string, any>).current_period_end as number'

Diff:
- const periodEnd = sub.current_period_end
+ const periodEnd = (sub as Stripe.Subscription & Record<string, any>).current_period_end as number

✓ Verified — a test passed after the fix.

Other candidates (4)
• 2 weeks ago: Type error: Module '"@/lib/utils"' has no exported member 'getInitials'.
• 2 weeks ago: Type error: Property 'role' does not exist on type 'User'.
```

That's one local command. No API call. The fix came from a session file Claude Code wrote to your disk weeks ago and Longhand had been waiting with the answer the whole time.

---

## MCP Integration (Claude Desktop)

Run `longhand mcp install` to wire Longhand into Claude Desktop's config. After you restart Claude Desktop, it has sixteen tools:

**Core (searchable archive):**
- `search` — semantic search with session, project, tool, file, and event_type filters (all combinable)
- `list_sessions` — recent sessions with project/time filters
- `get_session_timeline` — chronological view with offset/tail pagination and summary-only scan mode
- `replay_file` — reconstruct file state at a point in time
- `get_file_history` — every edit to a file across all sessions
- `get_stats` — storage statistics

**Proactive memory:**
- `recall` — fuzzy natural-language recall (use this first)
- `recall_project_status` — "where did we leave off on X?" — git-aware project summary with commits, issues, last outcome
- `search_in_context` — find something in a session and get the surrounding conversation
- `match_project` — find projects by partial name / category / description
- `find_episodes` — structured search for problem→fix pairs
- `get_episode` — full detail for one episode including diff + file state
- `list_projects` — browse inferred projects (compact by default, verbose optional)
- `get_project_timeline` — session-level timeline for one project

**Git history:**
- `get_session_commits` — all git operations in a session (commits, pushes, checkouts, merges)
- `find_commits` — search across all sessions by commit message, hash prefix, or branch name

All tools support `max_chars` output capping with pagination hints. No more 96k dumps crashing your context.

Once installed, you can ask Claude things like *"what did we decide about the auth middleware in last week's session?"* and it will actually search its own past work.

---

## Auto-Ingest

`longhand hook install` adds a `SessionEnd` hook to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionEnd": [
      {"command": "longhand ingest-session --transcript \"$CLAUDE_TRANSCRIPT_PATH\""}
    ]
  }
}
```

Every Claude Code session you have from that point forward will be automatically ingested and analyzed when it ends. Non-blocking. Runs in one to two seconds. You don't have to think about it again.

---

## Architecture

```
longhand/
├── parser.py              — JSONL → typed Events, nothing lost
├── replay.py              — deterministic file state reconstruction
├── types.py               — Pydantic models
├── storage/
│   ├── migrations.py      — version-aware schema evolution
│   ├── sqlite_store.py    — structured data + full raw JSON preserved
│   ├── vector_store.py    — ChromaDB (events + sessions + projects collections)
│   └── store.py           — unified ingest pipeline
├── extractors/            — per-event (errors, file refs, topics, git ops)
├── analysis/              — per-session (project, outcomes, episodes, embeddings)
├── recall/                — per-query (time parsing, project match, narrative)
├── cli.py                 — Typer CLI with Rich output
├── mcp_server.py          — Model Context Protocol server (16 tools)
└── setup_commands.py      — hook install, mcp install, config, doctor
```

**Source of truth:** SQLite. Every event's raw JSON is preserved as a blob. ChromaDB is the search index — it only holds what's needed for semantic retrieval.

**Analysis layer:** Runs at ingest time, not query time. Pre-computes projects, session outcomes, and episodes so recall queries are fast. Fully deterministic, no LLM.

**Recall pipeline:** `query → time parse → project match → episode search → rank → load artifacts → narrative`. Target latency under 200ms on a warm database.

---

## Comparison

|                          | Longhand                   | Summary-based (Mem0, MemPalace, LangMem) |
|--------------------------|----------------------------|------------------------------------------|
| Source                   | Raw Claude Code JSONL      | AI-generated summaries                   |
| Tool calls captured      | Every one, verbatim        | Whatever the summarizer kept             |
| File edits               | Full before/after diffs    | Usually not captured                     |
| Thinking blocks          | First-class events         | Usually discarded                        |
| File state replay        | Deterministic              | Not possible                             |
| Problem→fix extraction   | Rules-based, at ingest     | Depends on summarizer                    |
| Fuzzy recall             | Yes, with artifacts        | Text search over summaries               |
| What gets "decided"      | Nothing — store everything | The AI decides what matters              |
| Local-first              | Yes                        | Most                                     |
| Completeness             | Every event from the session file | Whatever the summarizer kept             |
| LLM calls to function    | Zero                       | Varies                                   |

Summary memory and Longhand solve different problems. Summary memory is good for long-term personal assistants that need compressed context across many conversations. Longhand is good for developers who need forensic access to their past Claude Code work — the kind of access where you need the exact diff, not a paraphrase.

---

## Stats

Tested end-to-end on a real Claude Code history:
- 107 unique sessions
- 53,668 events
- 19,252 tool calls
- 3,200 file edits
- 224 thinking blocks
- 37 projects inferred automatically
- 376 problem→fix episodes extracted (76 resolved)
- 299 conversation segments (design, story, debugging, discussion, planning)
- 665 git operations extracted (22 commits linked)
- 49,637 vectors indexed
- Vector search: ~126ms
- SQL queries: <30ms
- Storage footprint: ~1.3MB per session file (SQLite + Chroma combined)

---

## Token budget

The single most common question: *does Longhand consume a lot of tokens when Claude uses it?*

**No.** Every MCP tool has a hard output cap enforced in `longhand/mcp_server.py`. The response truncates and appends a pagination hint before Claude ever sees it, so the token cost per tool call is bounded — not by your history size, but by the cap itself.

| Tool | Default output cap | Rough token equivalent |
|---|---:|---:|
| `search` | 12,000 chars | ~3,000 tokens |
| `recall`, `get_session_timeline`, `get_latest_events`, `get_session_commits`, `find_commits` | 12,000–16,000 chars | ~3,000–4,000 tokens |
| `search_in_context` | 20,000 chars | ~5,000 tokens |
| Absolute ceiling (`MAX_OUTPUT_CHARS`) | 200,000 chars | ~50,000 tokens |

**Why this matters — the comparison:**

- **Reading one raw session JSONL directly:** 50K–200K tokens per session (Claude Code sessions are typically 1–5MB each).
- **Bigger-context-window approaches:** every prompt pays the full history, every time.
- **Summarizer-based memory tools:** cheap per-query but they already threw away the thinking blocks.

Longhand is flat-cost: the cap is per-call, not per-corpus. Recalling across 10 sessions and recalling across 1,000 sessions both come back in the same token envelope. And **Longhand itself makes zero API calls** — the only tokens consumed are the MCP payload Claude reads back. No model sits between you and your data.

**Tuning:** every tool accepts a `max_chars` parameter that can be lowered per-call. `summary_only: true` on timeline tools drops the `content` field and shrinks payloads ~10×.

---

174 unit tests passing. All 17 MCP tools stress-tested. Full security audit: zero critical findings, zero high findings. `~/.longhand/` created with 0700 permissions, all SQL parameterized, all inputs bounded. Dependencies: chromadb, typer, rich, pydantic, mcp.

---

## Author

Nate Nelson. Idaho Falls. No computer science degree. Fourteen industries of building software by describing what I see and letting the translation happen.


GitHub: [Wynelson94](https://github.com/Wynelson94)

---

## License

MIT. Do whatever you want with it.
