# Longhand

**Lossless local memory for Claude Code sessions. The full, unabbreviated version.**

Every tool call. Every file edit. Every thinking block. Every before-and-after. Stored verbatim. Searchable. Replayable. All on your machine. No API calls. No summaries. No decisions made by an AI about what's worth remembering.

---

## Why I Built This

I'm not a computer scientist. I'm a guy from Idaho Falls who's worked in fourteen industries and got a reputation as the person you hire when something's off but nobody can prove it.

At 28 I walked into a powersports store as the new GM. A family had been running it for thirty years — father, wife, daughter, son-in-law. The books looked clean. The bank statements reconciled. A fraud detective came in with specialized software and said there was nothing. I went line by line — bank statements, vendor invoices, card terminal logs, inventory counts — for four months at 19 hours a day. I found $400k in fraud traceable down to the date and time. The bank later found $7 million in missing collateral underneath. The operation was sophisticated. Every year they'd migrate to a new CRM and reformat the reports — burning the trail on purpose.

The principle I learned there is the same principle behind everything I build: **matter can't be created or destroyed. If something's missing, it moved. You just have to find the move.**

Information works the same way. It doesn't disappear — it gets compressed, summarized, or filed where nobody's looking. Find the raw data and you find the truth.

Most AI memory systems summarize conversations and then call the summary "memory." The AI decides what's important and throws the rest away. That's the same thing as letting a family run a business and also being the one counting the inventory. The audit is only as good as the auditor's incentives.

Longhand does it differently. It reads the actual session files Claude Code writes to disk — the complete JSONL transcripts — and stores every event verbatim in a local database you own. No summaries. No compression loss. No AI deciding what matters. Just the raw record, indexed for search and replay.

---

## What It Actually Does

When you use Claude Code, every session writes a JSONL file to `~/.claude/projects/<project>/<session-id>.jsonl`. That file contains:

- Every message you typed
- Every message Claude sent back
- Every thinking block Claude produced
- Every tool call Claude made (Edit, Write, Bash, Read, WebFetch, etc.)
- Every tool result
- The before and after content for every file edit

Longhand ingests those files into a local SQLite database and a local ChromaDB vector store. Then it gives you:

- **Semantic search** — "find every session where I debugged a race condition"
- **Tool call archaeology** — "show me every Bash command I've ever run that touched Supabase"
- **File history across sessions** — "show me every edit ever made to `route.ts`"
- **Session replay** — "reconstruct what `deals/[id]/pay/route.ts` looked like 30 minutes into the April 8 session" — computed by applying every edit verbatim from the session file
- **Reasoning retrieval** — "what was I thinking when I decided to use conditional updates for deal status transitions?" pulls the actual `thinking` block Claude produced at that moment
- **Timeline view** — chronological playback of any session
- **MCP server** — lets Claude query Longhand directly during live sessions

None of this is possible with summary-based memory. All of it is possible because Longhand never throws anything away.

---

## Install

```bash
# Core install (CLI + storage)
pip install longhand

# With MCP server for Claude Desktop / Claude Code integration
pip install "longhand[mcp]"
```

---

## Quick Start

```bash
# Ingest every Claude Code session on your machine
longhand ingest

# See what's indexed
longhand stats

# List recent sessions
longhand sessions

# Semantic search across everything
longhand search "race condition fix"

# Search only tool calls
longhand search "supabase migration" --type tool_call

# Search only thinking blocks (the reasoning Claude didn't show you)
longhand search "why did we choose" --type assistant_thinking

# Show a session timeline
longhand timeline <session-id-prefix>

# Reconstruct a file at the end of a session
longhand replay <session-id> /path/to/file.ts

# Show the full before/after of a single edit
longhand diff <event-id>

# Show overall stats
longhand stats
```

Session IDs accept prefix matches — `longhand timeline cf86` is enough if only one session starts with that.

---

## MCP Integration (Claude Desktop)

After installing with `pip install "longhand[mcp]"`, add Longhand to your Claude Desktop config:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "longhand": {
      "command": "python",
      "args": ["-m", "longhand.mcp_server"]
    }
  }
}
```

Restart Claude Desktop and it'll have access to six new tools:
- `search` — semantic search across all your Claude Code history
- `list_sessions` — recent indexed sessions
- `get_session_timeline` — chronological view of a session
- `replay_file` — reconstruct file state at a point in time
- `get_file_history` — every edit ever made to a file
- `get_stats` — storage statistics

Now you can say things like *"what did we decide about the auth middleware in last week's session?"* and Claude will actually search its own past work.

---

## Architecture

```
longhand/
├── parser.py       — JSONL → typed Events (no information loss)
├── types.py        — Pydantic models: Event, Session, FileState
├── storage/
│   ├── sqlite_store.py  — structured data + full raw JSON preserved
│   ├── vector_store.py  — ChromaDB semantic search
│   └── store.py         — unified interface
├── replay.py       — file state reconstruction engine
├── cli.py          — Typer-based CLI with Rich output
└── mcp_server.py   — Model Context Protocol server
```

**Storage philosophy:** SQLite is the source of truth. ChromaDB is the search index. Every event's raw JSON is preserved in SQLite so nothing is ever lost — the vector store only holds what's needed for semantic retrieval.

**Replay philosophy:** File state is reconstructed by finding the most recent `Write` tool call that precedes the target point, then applying every `Edit` / `MultiEdit` in sequence up to the target. The `old_string` and `new_string` for every edit are captured verbatim from the session JSONL — no guessing, no diff inference, just literal application.

---

## How It Compares to Other AI Memory Systems

| | Longhand | Summary-based (Mem0, MemPalace, LangMem) |
|---|---|---|
| **Source** | Raw Claude Code JSONL | AI-generated summaries |
| **Tool calls captured** | Every one, verbatim | Whatever the summarizer decided to keep |
| **File edits** | Full before/after diffs | Usually not captured |
| **Thinking blocks** | Yes, verbatim | Usually discarded |
| **File state replay** | Yes, deterministic | No |
| **What gets "decided"** | Nothing — everything is stored | The AI decides what matters |
| **Local-first** | Yes | Most are, some aren't |
| **Lossless** | Yes | No |

Summary-based memory asks: *"What should I remember about this conversation?"* and then throws the rest away.

Longhand asks: *"What actually happened?"* and keeps the whole record.

Both approaches work. They solve different problems. Summary memory is good for long-term personal assistants that need compressed context. Longhand is good for developers who need forensic access to their past work.

---

## Philosophy

I built a CRM, a compression system, a fraud investigation, and a number system for understanding people. All of them share one principle: **information doesn't disappear. It moves. You just have to find the move.**

Longhand is that principle applied to AI memory. The information Claude produces in your sessions doesn't need to be summarized to be useful. It needs to be findable. That's a different problem with a different solution.

The powersports family cooked the books by migrating to a new CRM every year and letting the old data become unreadable. Summary-based memory is the same shape: make the old data unreadable by design, then tell yourself the summary is the truth. I don't think that's the right approach for a tool you'll want to trust in five years when something breaks and you need to know why.

Store everything. Make it findable. Trust the raw record.

---

## Who Built This

Nate Nelson at **BlackSheep OI** in Idaho Falls. No computer science degree. Fourteen industries of business experience. Founder of a company working on lossless compression at the information-theoretic level — Longhand is one of the first public applications of that thesis.

**Non Gregis. Not of the herd.**

If you need systems like this built — forensic tools, data integrity audits, lossless memory layers, or anything where the official answer doesn't match reality — I'm available for hire.

- GitHub: [Wynelson94](https://github.com/Wynelson94)
- Company: [BlackSheep OI](https://blacksheephq.ai)

---

## License

MIT. Do whatever you want with it. If it saves your ass, let me know.
