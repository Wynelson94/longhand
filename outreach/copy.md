# Longhand Submission Copy

All ready-to-paste copy. Each section is numbered to match the checklist in `README.md`.

---

## §1 — Directory descriptions

### Short (1 line, ~140 chars)
> Persistent local memory for Claude Code. Every session event stored verbatim in SQLite + ChromaDB. Zero API calls. Semantic recall in ~126ms.

### Medium (2–3 sentences, forms / MCP directories)
> Longhand captures every tool call, file edit, and thinking block from every Claude Code session into a local SQLite + ChromaDB store. No summaries, no API calls, no AI deciding what matters — just lossless storage and ~126ms semantic recall across your entire history. Integrates as an MCP server giving Claude 16 tools to query its own past.

### Long (for awesome-list entries, PR descriptions)
> **[Longhand](https://github.com/Wynelson94/longhand)** — Persistent local memory for Claude Code. Indexes every session file (tool calls, edits, thinking blocks) into a local SQLite + ChromaDB store for lossless, offline semantic recall. Integrates as an MCP server with 16 tools. Zero API calls, zero summarization. ~126ms queries across 100+ sessions.

### Awesome-list bullet (markdown, one-line)
```markdown
- [Longhand](https://github.com/Wynelson94/longhand) - Persistent local memory for Claude Code. Lossless capture of every session event into SQLite + ChromaDB. Zero API calls, ~126ms semantic recall, 16 MCP tools. MIT.
```

### Keywords / tags (paste into forms asking for tags)
`claude-code, mcp, memory, local-first, semantic-search, sqlite, chromadb, ai-tools, developer-tools, cli, offline, privacy, python`

---

## §2 — Show HN post

**Title:**
> Show HN: Longhand – Lossless local memory for Claude Code (no summaries, no API)

**URL field:**
> https://github.com/Wynelson94/longhand

**Text field:**
```
Hi HN — I built Longhand because every AI memory tool I tried summarized my sessions before giving them back to me. Summarization is a lossy decision disguised as a convenience: an LLM decides what's worth remembering, and I never get to see what it threw away.

Longhand does the opposite. Claude Code already writes every tool call, every file edit, every thinking block to JSONL files on disk. Longhand reads those files and indexes them verbatim into a local SQLite + ChromaDB store. Nothing is summarized. Nothing is sent through an API. Semantic recall across 100+ sessions returns in ~126ms.

A few design notes:

- Claude Code rotates those JSONL files off disk after a few weeks. If you install Longhand late, the past before install is gone. Install early.
- It exposes 16 MCP tools to Claude itself, so the model can query its own history ("that stripe webhook fix from last week") without eating tokens on stale context.
- Storage is ~1GB for a heavy user (120+ sessions, 60k events), 200–400MB typical.
- Python 3.10–3.13 fully supported; 3.14 works with a chromadb<1.0 pin (upstream segfault).

The "AI memory crisis" seemed artificial to me. SQLite is from 2000. ChromaDB is two years old. Both run on a laptop. The memory doesn't need to live in the model — it needs to live on the disk.

`pip install longhand` → `longhand setup` → it ingests your history, installs the hooks, and registers as an MCP server. Repo: https://github.com/Wynelson94/longhand

Happy to answer questions about the architecture, the decision to skip summarization entirely, or the tradeoffs vs larger context windows.
```

**Timing:** Post Tue or Wed, 8–10am ET. Stay on thread for 4+ hours to answer questions.

---

## §3 — r/ClaudeAI post

**Title:**
> I built persistent local memory for Claude Code — no API calls, lossless replay of every session

**Body:**
```
TL;DR: `pip install longhand` → `longhand setup` → Claude Code now remembers every session you've ever had, queryable in ~126ms. Free, open source, 100% local.

**The problem I kept hitting:** Claude Code writes rich JSONL logs of every session — tool calls, file edits, thinking blocks — into `~/.claude/projects/`. Then quietly rotates them off disk after a few weeks. Every memory tool I tried summarized those sessions before giving them back to me, which meant I never saw what got dropped.

**What Longhand does:**
- Reads the JSONL files verbatim into SQLite + ChromaDB
- Nothing is summarized. Nothing goes through an API.
- Registers as an MCP server with 16 tools, so Claude can query its own history
- Semantic recall works: "that webhook bug from last week" → returns the actual conversation, the actual edit, the actual fix
- ~126ms queries on my 107-session / 53k-event store

**Install is one command:**
```
pip install longhand
longhand setup
```

Repo: https://github.com/Wynelson94/longhand — MIT, 170 tests, security-audited (zero critical findings).

Happy to answer questions. Also curious what other people's session counts look like — I'm at 107 and rising fast.
```

---

## §4 — r/LocalLLaMA post

**Title:**
> Lossless, 100%-local memory for Claude Code — zero API calls, SQLite + ChromaDB on disk

**Body:**
```
This is Claude Code-specific, but the philosophical alignment with this sub is why I'm posting here.

Longhand is a memory layer for Claude Code that is aggressively local:
- Zero API calls, ever
- No summarization (every tool call, edit, and thinking block stored verbatim)
- Data lives on your disk forever — never touches a vendor's servers
- Semantic recall runs against a local ChromaDB index (~126ms)
- Works fully offline once installed

The reason I built it: every memory tool in this space assumes "memory" means "ask an LLM to summarize your past." That's a lossy, API-dependent, vendor-locked design. I wanted the opposite — the disk carries the memory, the model just queries it.

Storage footprint: ~1GB for a heavy user (60k events across 120+ sessions). 200–400MB typical. Once Claude Code rotates its own session files off disk, Longhand is the only copy.

It exposes itself as an MCP server (16 tools), so Claude Code can query its own past without eating tokens on stale context.

Install:
```
pip install longhand
longhand setup
```

Repo: https://github.com/Wynelson94/longhand — MIT. 170 tests passing. Python 3.10–3.13 fully supported.

Not a drop-in for LocalLLaMA setups since it hooks Claude Code specifically, but the design philosophy (local-first, no API, no summarization) is something this community usually appreciates — and the architectural patterns (SQLite + local vector store + verbatim capture) generalize cleanly to any AI session log.
```

---

## §5 — X/Twitter thread

**Tweet 1/5:**
```
Everyone is solving AI memory by making context windows bigger.

1M tokens. 2M tokens. "Context-infinite."

I built Longhand by going the other direction: the model doesn't need to carry the memory. The disk does.

A thread on what it means in practice ↓
```

**Tweet 2/5:**
```
Claude Code writes every session — tool calls, file edits, thinking blocks — to JSONL files on disk.

Then quietly rotates them off disk after a few weeks.

Longhand reads them verbatim into SQLite + ChromaDB before they're gone. Nothing summarized. Nothing sent to an API.
```

**Tweet 3/5:**
```
Key numbers:

• Semantic recall: ~126ms across 100+ sessions
• Storage: ~1GB for a heavy user, 200–400MB typical
• API calls: 0
• Summarization: 0
• Vendor lock-in: 0
• 16 MCP tools exposed to Claude itself
```

**Tweet 4/5:**
```
The "AI memory crisis" was an artificial constraint.

SQLite is from 2000. ChromaDB is two years old. Both run on a laptop.

Longhand bypasses the crisis by ignoring it — your past sessions are already on disk, written by Claude Code itself. Indexing them locally is a solved problem.
```

**Tweet 5/5:**
```
pip install longhand
longhand setup

MIT. Python 3.10–3.13. 170 tests. Security-audited (zero critical findings). On PyPI.

Repo: https://github.com/Wynelson94/longhand

Tagging @AnthropicAI + anyone building on MCP — would love your thoughts.
```

**Who to tag:** @AnthropicAI, @alexalbert__, @swyx, @cline (if they're on X), @zeddotdev, @continuedev, @BenjaminDEKR (Glama), plus any MCP devs you've seen on X.

---

## §6 — Dev.to / blog post

**Title:** Why I built a lossless alternative to AI memory summarization

**Subtitle:** The disk doesn't need an LLM to decide what's worth remembering.

**Outline (write in this order):**

1. **Hook** — The moment I realized every memory tool I tried was summarizing my sessions, and I never got to see what got dropped.
2. **The industry's direction** — Bigger context windows. 1M tokens. 2M tokens. Paragraph on why this is the wrong axis.
3. **The actual state of the world** — Claude Code already writes rich JSONL logs of every session. They exist. On your disk. Right now.
4. **The problem** — Claude Code rotates those files off disk after a few weeks. Most memory tools summarize them. Both paths are lossy.
5. **The architecture** — SQLite for structured events. ChromaDB for semantic search. Hooks for auto-ingestion. MCP for exposure back to Claude.
6. **The numbers** — 126ms recall. 1GB max storage. Zero API calls. 170 tests.
7. **The contrarian framing** — "Summarization is a lossy decision disguised as a convenience."
8. **What it unlocks** — Cross-model portability. Offline work. No vendor lock-in. Forensic replay of any past decision.
9. **What it doesn't try to do** — Not a general-purpose memory. Specific to Claude Code's JSONL format. Won't help you with ChatGPT.
10. **Install line + repo link.**

Target length: 800–1,200 words. Include the comparison table from the README. Link the repo at top and bottom. Cross-post to Medium + Hashnode day-of.

---

## §8 — v0.9.2 second-push copy (2026-05-17)

The April launch (§§1–7 above) sent the curve up then it decayed (175/wk vs 733 peak). The v0.9.2 push reuses the same channels but leads with a different hook: **`longhand demo`** — try it on a fake corpus in 60 seconds, no install commitment, no touching your real `~/.claude`.

Updated stats (use these everywhere instead of the launch numbers):
- **19 MCP tools** (was 16) — `outputSchema` + `annotations` added in v0.9.1
- **228 tests passing** (was 170) — +6 demo tests in v0.9.2
- **GLAMA A-tier** scores · **SafeSkill 93/100** — both as badges in README
- **Live ingestion** since v0.9.0 (Stop hook + reconciler)
- **PyPI weekly installs**: 175 currently (peak 733 mid-April)

### §8.1 — Short summary (universal, 1 line)
> Lossless local memory for Claude Code. Try `longhand demo` in 60s — sandboxed, no install commitment.

### §8.2 — Show HN v0.9.2

**Title:**
> Show HN: Longhand v0.9.2 — try lossless local memory for Claude Code without installing it

**URL:**
> https://github.com/Wynelson94/longhand

**Text:**
```
A few of you saw the v0.9.0 launch in April. Since then a question kept coming up: "How do I know this is going to work on my actual session history before I run setup on my real ~/.claude?"

v0.9.2 ships `longhand demo` — one command that creates a sandboxed store under /tmp, seeds it with three fake Claude Code sessions (a Stripe webhook bug, a Supabase auth migration, a downstream 401 fix), then walks you through `recall`, cross-session retrieval, and `recall_project_status` so you see the actual output before pointing Longhand at your own data. Cleans up afterwards; --keep leaves it for exploration.

  pip install longhand
  longhand demo

The original premise is unchanged: Claude Code writes every tool call, file edit, and thinking block to JSONL on disk. Longhand reads them verbatim into SQLite + ChromaDB. Zero API calls. Zero summarization. ~126ms semantic recall across 100+ sessions. 19 MCP tools so Claude can query its own past.

What landed since April:
- Live ingestion (v0.9.0) — sessions show up in recall while you're still working, not at SessionEnd
- Plan history as first-class data — every Write/Edit to ~/.claude/plans/*.md is indexed
- Reconciler launchd job (opt-in) — belt-and-suspenders for hard crashes
- 19 MCP tools (was 16) with annotations + outputSchema — v0.9.1 quality pass
- 228 tests passing across a 4-matrix Python CI (3.10–3.13)

Repo: https://github.com/Wynelson94/longhand (MIT). GLAMA A-tier, SafeSkill 93/100.

Happy to answer questions about the demo, the no-summarization design, the architecture, or why Claude's own JSONL format is the right starting point instead of a custom event log.
```

**Timing:** Tue/Wed 8–10am ET. Stay on thread 4+ hours.

### §8.3 — mcp.so / mcpservers.org / directory submission (medium)

> **Longhand** — Lossless local memory for Claude Code. Indexes every session file (tool calls, edits, thinking blocks) verbatim into SQLite + ChromaDB. 19 MCP tools for Claude to query its own past. Zero API calls, ~126ms recall. `pip install longhand && longhand demo` to try it on a sandboxed corpus without touching your real data. MIT, Python 3.10–3.13, 228 tests, GLAMA A-tier.

### §8.4 — awesome-mcp-servers PR bullet

```markdown
- [Longhand](https://github.com/Wynelson94/longhand) - Persistent local memory for Claude Code. Lossless capture of every session event into SQLite + ChromaDB. 19 MCP tools, ~126ms recall, zero API calls. Try with `longhand demo` (sandboxed). MIT.
```

### §8.5 — awesome-claude-code PR bullet (resubmission)

The previous submission was closed Apr 15 for "repo too young" and blocked by an unrelated Shipwright issue. Resubmission should be unblocked now (eligible after Apr 16; Shipwright should have moved). Use this entry:

```markdown
- [Longhand](https://github.com/Wynelson94/longhand) — Persistent local memory layer for Claude Code. Hooks into SessionEnd and Stop, indexes every tool call/edit/thinking block into local SQLite + ChromaDB. Exposes 19 MCP tools so Claude can query its own past. `longhand demo` for a no-install sandboxed walkthrough.
```

### §8.6 — X/Twitter follow-up thread (post-release)

**Tweet 1/4:**
```
v0.9.2 shipped.

The friction I kept hearing: "I want to try it but not on my real ~/.claude history."

New `longhand demo` command — sandboxed walkthrough on a fake corpus in 60 seconds. No install commitment.

  pip install longhand
  longhand demo
```

**Tweet 2/4:**
```
Since the v0.9.0 launch in April:

• Live ingestion (Stop hook) — sessions show up in recall WHILE you're still working
• Plan history as first-class data
• Reconciler launchd job (opt-in) for hard crashes
• 19 MCP tools (was 16) with annotations + outputSchema
• SafeSkill 93/100, GLAMA A-tier

228 tests across Python 3.10–3.13.
```

**Tweet 3/4:**
```
The design argument hasn't changed.

Claude Code already writes every tool call, every file edit, every thinking block to JSONL. Longhand reads those files verbatim. SQLite + ChromaDB on your laptop. No API. No summaries. No vendor.

"AI memory crisis" was solvable in 2000.
```

**Tweet 4/4:**
```
Try it without committing anything:

  pip install longhand
  longhand demo

Or wire it to your real Claude Code:

  longhand setup

MIT. https://github.com/Wynelson94/longhand
```

### §8.7 — Newsletter pitch (v0.9.2)

```
Hi [name],

Quick update on Longhand — the lossless local memory layer for Claude Code I pitched after the April launch. v0.9.2 ships today with a `longhand demo` command that runs a sandboxed walkthrough on a fake corpus in 60 seconds, so users can preview the cross-session recall behavior without committing to install on their real session history.

For [newsletter name] readers, the angle that might resonate: every other "AI memory" tool in the space asks an LLM to summarize your past. Longhand does the opposite — Claude Code already writes verbatim JSONL of every session; Longhand just indexes those files into local SQLite + ChromaDB. Zero API calls. Zero summarization. 19 MCP tools exposed back to Claude itself.

Since April: 733/wk → 175/wk PyPI installs (organic only; no paid distribution). v0.9.0 added live ingestion + plan history + reconciler. v0.9.1 added MCP tool annotations + outputSchema for SafeSkill 93/100 + GLAMA A-tier. 228 tests across Python 3.10–3.13.

Repo: https://github.com/Wynelson94/longhand
PyPI: https://pypi.org/project/longhand/

Open to whatever format works — quick mention, guest piece, AMA, or just a quote for a roundup.

— Nate Nelson
BlackSheep OI
```

---

## §7 — Newsletter pitch template

**Subject:** Longhand — lossless local memory for Claude Code, no API calls

**Body:**
```
Hi [name],

I shipped Longhand last month and it's picked up real traction — 336 unique cloners and 733 PyPI installs in the last 14 days, accelerating week-over-week. Thought it might fit [newsletter name].

Longhand is a local-first memory layer for Claude Code: every tool call, file edit, and thinking block from every session captured verbatim into SQLite + ChromaDB. Zero API calls, zero summarization. 16 MCP tools exposed back to Claude so it can query its own history in ~126ms.

The design argument: the "AI memory crisis" is artificial. SQLite is from 2000, ChromaDB is two years old, both run on a laptop. The model doesn't need to carry memory — the disk does.

Repo: https://github.com/Wynelson94/longhand
PyPI: https://pypi.org/project/longhand/

Happy to write a guest piece, do an AMA, or just feed you a quote for a roundup. Whatever fits the format.

— Nate Nelson
BlackSheep OI
```

Customize subject + intro line per newsletter. Keep body ~130 words.
