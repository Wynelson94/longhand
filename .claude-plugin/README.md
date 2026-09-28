# Longhand — Claude Code Plugin

Persistent local memory for Claude Code. Every tool call, every file edit, every thinking block from every session — stored verbatim on your machine. Recall any past fix, decision, or conversation in ~128ms median (measured 2026-09-27). Zero API calls. Nothing leaves your laptop.

## What this plugin does

Installing the Longhand plugin gives Claude Code 13 MCP tools for searching and replaying your session history, including:

- **`recall`** — fuzzy, proactive recall for "do you remember when..." questions. Returns matching projects, episodes, and narrative in one call.
- **`recall_project_status`** — git-aware "where did we leave off on X" across recent commits, unresolved issues, and the last session's outcome.
- **`search`** — semantic search, with surrounding conversation when you pass `context_events`.
- **`get_file_history`** / **`replay_file`** — reconstruct any file's state at any point.
- **`find_episodes`** — structured problem → fix retrieval with the exact diffs that resolved it; pass `episode_id` for full detail on one.

Full tool reference: [github.com/Wynelson94/longhand/wiki](https://github.com/Wynelson94/longhand/wiki)

## Prerequisite

Longhand is a Python package published to PyPI. Install it once before enabling the plugin:

```bash
pip install longhand
longhand setup
```

`longhand setup` is idempotent — it backfills your existing `~/.claude/projects/` history, installs the hooks so new and in-progress sessions auto-ingest, registers Longhand's MCP server for Claude Desktop, and verifies everything works. This plugin is what gives *Claude Code* the same 13 tools, via the bundled MCP configuration below — `setup` itself doesn't need to touch Claude Code's config for that.

If the plugin is enabled without the CLI installed, you'll see a clear `SessionStart` message telling you what to run.

## Why another memory tool

Longhand takes the opposite architectural stance from AI-summarization tools like `claude-mem`:

|                     | Summarization tools           | Longhand                       |
| ------------------- | ----------------------------- | ------------------------------ |
| What's stored       | AI-generated summaries        | Verbatim events from raw JSONL |
| Who decides         | An LLM, at write time         | Nobody — everything is kept    |
| API calls / session | One or more                   | Zero                           |
| Thinking blocks     | Usually folded into summaries | First-class, stored verbatim   |
| Model portability   | Tied to summarizer's output   | Same data works across models  |

Longhand's thesis: **the model doesn't need to carry the memory — the disk does.** Your Claude Code sessions are already written to `~/.claude/projects/` as JSONL. Longhand just reads that file, indexes it locally in SQLite + ChromaDB, and gives you fuzzy semantic recall over your entire history.

## Validated against real sessions

Tested against 792 real sessions (786 Claude Code, 6 Codex) / 269,067 events / 3,723 git operations / 1,809 problem-fix episodes across 45 inferred projects (measured 2026-09-27). 617 unit tests passing. Security-audited with zero critical findings.

## Links

- **Source:** https://github.com/Wynelson94/longhand
- **PyPI:** https://pypi.org/project/longhand/
- **Docs:** https://github.com/Wynelson94/longhand/wiki
- **License:** MIT
