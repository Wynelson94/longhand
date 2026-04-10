# Longhand

**Lossless local memory for Claude Code. The full, unabbreviated version.**

Every tool call. Every file edit. Every thinking block. Every before-and-after. Stored verbatim on your machine. Searchable. Replayable. Recallable by fuzzy natural-language questions. Zero API calls. Zero summaries. Zero decisions made by an AI about what's worth remembering.

---

## Persistent Memory Without Tokens

Everyone is solving AI memory by making the context window bigger. 1M tokens. 2M tokens. Context-infinite. The whole industry is racing in the same direction: make the model carry more state.

Longhand goes the other direction. **The model doesn't need to carry the memory. The disk does.**

|                      | Bigger context windows                       | Longhand                          |
|----------------------|----------------------------------------------|-----------------------------------|
| **Where it lives**   | Rented from a model provider                 | A SQLite file + ChromaDB on your laptop |
| **Cost per query**   | Tokens × dollars                             | Zero                              |
| **Privacy**          | Goes through someone else's servers          | Never leaves your machine         |
| **Speed**            | Seconds to minutes for large contexts        | ~126ms                            |
| **Loss**             | Attention degrades in the middle of long contexts | Forensically lossless        |
| **Persistence**      | Dies when the window closes                  | Lives until you delete the file   |
| **Across model versions** | Doesn't transfer                        | Same data, any model              |
| **Offline**          | No                                            | Yes                               |
| **Scales with**      | Provider's pricing                           | Your hard drive                   |

The "memory crisis" in AI was an artificial constraint. Storage is solved. SQLite is from 2000. ChromaDB is two years old. Both run on a laptop. Longhand bypasses the crisis by ignoring it — your past sessions are already on disk, written by Claude Code itself, in JSONL files that contain every single event verbatim. Longhand reads those files, indexes them locally, and gives you semantic recall over your entire history without ever sending a token through someone else's API.

**More secure than tokens. Lossless. Yours.**

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
- **Filterable search** — by tool, file, session, time range, event type
- **Tool call archaeology** — "show me every Bash command I ran in March that touched Supabase"
- **File history across sessions** — every edit to a specific file, chronologically, across all your sessions
- **Session replay** — reconstruct any file's state at any point in any past session
- **Reasoning retrieval** — query Claude's verbatim thinking blocks
- **Timeline view** — chronological playback of any session
- **Fuzzy recall** — natural-language questions about past work ("that race condition fix from last week")
- **Project inference** — automatic detection of which projects you've worked on, with categories and aliases
- **Episode extraction** — automatic detection of problem→fix sequences in your sessions
- **MCP server** — lets Claude Desktop query Longhand directly during live conversations
- **Auto-ingest hook** — drops into Claude Code's `SessionEnd` hook so new sessions are indexed automatically

---

## Install

```bash
pip install -e .
```

Then make it current:

```bash
longhand ingest                # ingest all your existing Claude Code history
longhand analyze --all         # run analysis (projects, outcomes, episodes)
longhand hook install          # auto-ingest every future session
longhand mcp install           # let Claude Desktop call Longhand as MCP tools
longhand doctor                # verify everything is wired up
```

Those five commands take about two minutes the first time and zero maintenance after that.

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
longhand continue <session-id>              # pick up where I left off
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

# Export
longhand export latest-fix                  # most recent resolved episode
longhand export ep_<id> --out fix.md        # specific episode to file
longhand export <session-id-prefix>         # full session timeline
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

Run `longhand mcp install` to wire Longhand into Claude Desktop's config. After you restart Claude Desktop, it has thirteen tools:

**Core (searchable archive):**
- `search` — semantic search across all events
- `list_sessions` — recent sessions with filters
- `get_session_timeline` — chronological view of a session
- `replay_file` — reconstruct file state at a point in time
- `get_file_history` — every edit to a file across all sessions
- `get_stats` — storage statistics

**Proactive memory:**
- `recall` — fuzzy natural-language recall (use this first)
- `match_project` — find projects by partial name / category / description
- `find_episodes` — structured search for problem→fix pairs
- `get_episode` — full detail for one episode including diff + file state
- `list_projects` — browse inferred projects
- `get_project_timeline` — session-level timeline for one project

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
├── extractors/            — per-event (errors, file refs, topics)
├── analysis/              — per-session (project, outcomes, episodes, embeddings)
├── recall/                — per-query (time parsing, project match, narrative)
├── cli.py                 — Typer CLI with Rich output
├── mcp_server.py          — Model Context Protocol server (13 tools)
└── setup_commands.py      — hook install, mcp install, doctor
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
| Lossless                 | Yes                        | No                                       |
| LLM calls to function    | Zero                       | Varies                                   |

Summary memory and Longhand solve different problems. Summary memory is good for long-term personal assistants that need compressed context across many conversations. Longhand is good for developers who need forensic access to their past Claude Code work — the kind of access where you need the exact diff, not a paraphrase.

---

## Stats

Tested end-to-end on a real 721-file Claude Code history:
- 102 unique sessions
- 51,000 events
- 18,000 tool calls
- 3,000 file edits
- 224 thinking blocks
- 34 projects inferred automatically
- 74 problem→fix episodes extracted
- Vector search: ~126ms on 47k indexed events
- SQL queries: <30ms
- Storage footprint: ~1.3MB per session file (SQLite + Chroma combined)

45/45 unit tests passing. No external dependencies beyond chromadb, typer, rich, pydantic. Optional MCP support via `pip install "longhand[mcp]"`.

---

## Author

Nate Nelson. Idaho Falls. No computer science degree. Fourteen industries of building software by describing what I see and letting the translation happen.


GitHub: [Wynelson94](https://github.com/Wynelson94)

---

## License

MIT. Do whatever you want with it.
