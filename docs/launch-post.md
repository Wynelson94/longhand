# Why I built Longhand: the model doesn't need to carry the memory — the disk does

*Published as a GitHub Discussion: https://github.com/Wynelson94/longhand/discussions/3*
*Source of truth: this file in the repo. Edits go through git.*

---

The AI industry is solving memory by making the context window bigger. 1M tokens. 2M tokens. Context-infinite. Everyone is racing in the same direction: make the model carry more state.

I built **Longhand** to go the other way.

## The inversion

The model doesn't need to carry the memory. The disk does.

|                            | Bigger context windows                           | Longhand                                     |
| -------------------------- | ------------------------------------------------ | -------------------------------------------- |
| **Where it lives**         | Rented from a model provider                     | A SQLite file + ChromaDB on your laptop      |
| **Cost per query**         | Tokens × dollars                                 | Zero                                         |
| **Privacy**                | Goes through someone else's servers              | Never leaves your machine                    |
| **Speed**                  | Seconds to minutes for large contexts            | ~126ms                                       |
| **Loss**                   | Attention degrades in the middle of long inputs  | Every event from the source, nothing dropped |
| **Persistence**            | Dies when the window closes                      | Lives until you delete the file              |
| **Across model versions**  | Doesn't transfer                                 | Same data, any model                         |
| **Offline**                | No                                               | Yes                                          |
| **Scales with**            | Provider's pricing                               | Your hard drive                              |

The "memory crisis" in AI was an artificial constraint. Storage is solved. SQLite is from 2000. ChromaDB is two years old. Both run on a laptop.

Claude Code already writes every session to disk as JSONL. That file contains every message, every tool call, every thinking block, every file edit with full before/after content, every diff, every timestamp. It is the raw source. Longhand reads it, indexes it locally, and gives you semantic recall over your entire history without a single API call.

## What that looks like in practice

```
$ pip install longhand
$ longhand setup          # backfill history + install hooks + register MCP
$ longhand recall "that stripe webhook bug from last week"
```

Validated against 107 real Claude Code sessions — 53,668 events, 665 git operations, 376 problem-fix episodes, 299 conversation segments across 37 inferred projects. Vector search at ~126ms, SQL queries under 30ms, ~1.3MB of storage per session file.

## Why not AI summarization?

The dominant Claude Code memory tool on GitHub is [`thedotmack/claude-mem`](https://github.com/thedotmack/claude-mem) — 55k+ stars, and it's a good tool. It's also solving the problem in the opposite direction from Longhand.

|                                | claude-mem                                   | Longhand                                     |
| ------------------------------ | -------------------------------------------- | -------------------------------------------- |
| **What's stored**              | AI-generated summaries / "observations"      | Verbatim events from the raw JSONL           |
| **Who decides what's kept**    | An LLM, at write time                        | Nobody — everything is kept                  |
| **Compression**                | Semantic (lossy, by design)                  | None (lossless)                              |
| **API calls per session**      | One or more (calls Claude to summarize)      | Zero                                         |
| **Thinking blocks**            | Typically folded into summaries              | First-class, stored verbatim                 |
| **Deterministic replay**       | No — summaries can't reconstruct file state  | Yes — every diff kept and replayable         |
| **Model portability**          | Tied to the summarizer's output              | Same data works across any model, forever   |
| **Runtime**                    | TypeScript, Bun, HTTP worker on :37777       | Python, no server                            |
| **License**                    | AGPL-3.0                                     | MIT                                          |

The philosophical split: **claude-mem asks an AI what was important and stores that. Longhand stores the actual bytes and lets any future AI decide.**

Both can coexist on the same machine. They operate on the same JSONL files without interfering.

## The principles

Longhand is built on a handful of ideas. If you disagree with them, you probably want a different tool.

**1. Information doesn't disappear — it moves.** When data goes "missing" it's almost never actually gone. It got compressed, summarized, filed somewhere else, or renamed. Find the raw source and the truth is still there waiting.

**2. Summarization is a lossy decision disguised as a convenience.** Most AI memory systems ask the AI to write down "what mattered." The AI is now the gatekeeper of its own memory, and the AI has incentives — brevity, confidence, coherence — that aren't the same as truth. You end up with a story about what happened instead of what happened.

**3. The raw record is cheap.** A full Claude Code JSONL is kilobytes to low megabytes. A year of daily sessions is hundreds of megabytes. Summary-based memory isn't saving space — it's giving away information that was free.

**4. The thinking is the most valuable part.** Claude's `thinking` blocks are the reasoning behind the decision — usually invisible to the user, almost always more useful than the final answer. Summary-based memory throws them away. Longhand treats them as first-class events.

**5. A fix you can't reproduce is a fix you didn't keep.** If you fixed a bug in March, the state of that file when the bug was fixed is a fact. Longhand reconstructs it deterministically by applying every edit in sequence.

**6. Memory should be proactive, not just searchable.** Real memory answers fuzzy questions. "A couple months ago I was building a game that kept breaking, then you fixed it — bring that fix forward." Longhand parses the time phrase, matches the project, finds the episode, returns the diff.

**7. Deterministic beats clever.** Everything in the core pipeline is rules-based. Regex error detection. Hash-based project IDs. Forward-walking episode extraction. No LLMs in the hot path. Fast, reproducible, fully local.

**8. Local or nothing.** Your Claude Code history is yours. It lives in `~/.longhand/`. No telemetry. No sync. No account. If your laptop is offline, Longhand works.

## Try it

```bash
pip install longhand
longhand setup
longhand recall "what was that thing I fixed yesterday"
```

Full docs: [Longhand Wiki](https://github.com/Wynelson94/longhand/wiki).

Issues, questions, disagreements welcome on the [repo](https://github.com/Wynelson94/longhand).

— Nate
