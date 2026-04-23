---
title: My memory tool said "no session history." The session had 2,526 lines.
published: false
description: I built a local memory tool for Claude Code, then asked it to recall yesterday's work. It returned empty. Here's how I diagnosed, fixed, and audited my own tool using itself — shipping two releases in one session.
tags: claude, python, opensource, ai
cover_image:
---

*Source of truth for this post is the repo: [github.com/Wynelson94/longhand/blob/main/docs/devto-dogfood-post.md](https://github.com/Wynelson94/longhand/blob/main/docs/devto-dogfood-post.md). Edits go through git.*

---

Yesterday I asked Claude Code to pull up where we'd left off on a project I'd been working on a few hours earlier. It's a project called [bsoi-mesh-kit](https://github.com/Wynelson94/bsoi-mesh-kit) — a local STL validator I'm building for a service bureau. The recall tool I built, [Longhand](https://github.com/Wynelson94/longhand), is supposed to handle exactly this question.

The response came back:

> `recall_project_status("bsoi-mesh-kit")` → `"No session history found for this project."`

Except: there were **four JSONL transcripts on disk** for that project, including a **2,526-line** work session from earlier that day where I'd shipped three version bumps, invited a collaborator, and patched a Pantheon Slicer config bug. The session was real. Longhand had captured none of it.

The rest of this post is the diagnosis and the two releases that came out of it. It's written as a self-contained case study in building a tool that can catch itself in a lie.

## What Longhand is, in one paragraph

Longhand is a Python CLI + MCP server that reads Claude Code's session transcripts (`~/.claude/projects/**/*.jsonl`), indexes every tool call / file edit / thinking block into SQLite + ChromaDB, and exposes semantic recall via MCP tools. Zero API calls. Local-only. The pitch in one line: *the model doesn't need to carry the memory — the disk does.* The longer pitch [is here](https://github.com/Wynelson94/longhand/discussions/3). Installed on PyPI: `pip install longhand`.

## Step 1: confirm the failure is real

First thing I checked was the raw file system:

```bash
$ ls ~/.claude/projects/-Users-natenelson/ | grep -E "823dd358|002f6297|e6a3b13f"
002f6297-129e-4d09-b112-c48bd777e3ba.jsonl
823dd358-f32f-4d73-a481-38a05b378966.jsonl
e6a3b13f-3912-4ee3-b9aa-fa4fc509cb29.jsonl

$ wc -l ~/.claude/projects/-Users-natenelson/823dd358*.jsonl
    2526 /Users/natenelson/.claude/projects/-Users-natenelson/823dd358-f32f-4d73-a481-38a05b378966.jsonl
```

2,526 lines on disk. Now what does SQLite have?

```bash
$ sqlite3 ~/.longhand/longhand.db "
    SELECT session_id, project_path, project_id
    FROM sessions
    WHERE transcript_path LIKE '%823dd358%'
       OR transcript_path LIKE '%002f6297%'
       OR transcript_path LIKE '%e6a3b13f%';"

e6a3b13f-3912-4ee3-b9aa-fa4fc509cb29 | /Users/natenelson |
002f6297-129e-4d09-b112-c48bd777e3ba | /Users/natenelson |
```

Two things jumped out:

1. **The big session (`823dd358`) isn't in the `sessions` table at all.** Never ingested.
2. **The two shorter sessions are ingested but have `project_id = NULL`** and a `project_path` of `/Users/natenelson` — my home directory, not the project.

Two distinct failure modes in one dataset. Time to understand each.

## Root cause A: SessionEnd hook didn't fire on the big session

Longhand ingests new sessions via a Claude Code `SessionEnd` hook that runs `longhand ingest-session`. The hook was installed and pointed to the right binary. But `823dd358` — the most important session of the day — never got captured by it.

I don't know exactly why the hook didn't fire (Claude Code's exit paths are varied, and a few of them skip `SessionEnd`). What I know is **there was no retry, no log, no detection mechanism**. If a hook silently fails, the only way to notice is to manually query something that should have been there and find it missing.

That's the dogfood failure in one sentence: the tool that was supposed to give me observability into my past work silently lost an entire work session, and I only noticed because I happened to ask about that specific session the next day.

## Root cause B: project inference was using the first-event cwd

For the two sessions that *did* get ingested, the `project_id` was NULL because `project_path` was `/Users/natenelson`. Why?

Claude Code launched from my home directory. So the transcript's **first event** had `cwd=/Users/natenelson`. Later events — after I `cd`'d into the project — had `cwd=/Users/natenelson/Projects/bsoi-mesh-kit`. But Longhand's ingest pipeline only looked at the first event.

A quick scan of the big session confirmed the multi-cwd pattern:

```python
cwds = set()
for line in open('823dd358-....jsonl'):
    obj = json.loads(line)
    if c := obj.get('cwd'): cwds.add(c)

# => {
#   '/Users/natenelson',
#   '/Users/natenelson/Projects/bsoi-mesh-kit',
#   '/Users/natenelson/Projects/bsoi-ops',
# }
```

Any session where I `cd` between repos mid-session got misattributed. And since `recall_project_status` filters `WHERE project_id = ?`, NULL-project rows are invisible to it.

## The v0.6.0 fix

Four changes shipped together:

**1. Mode-of-cwd project inference.** Tally every event's `cwd`, filter out `$HOME` and any path that doesn't walk up to a project marker (`.git`, `pyproject.toml`, `package.json`, …), pick the mode. Multi-project sessions get attributed to the repo where most of the work happened.

```python
def _pick_best_project_cwd(events):
    home_resolved = Path.home().resolve()
    counts = Counter()
    resolved_cache = {}
    for e in events:
        cwd = e.cwd
        if not cwd or cwd in resolved_cache:
            if cwd in resolved_cache and resolved_cache[cwd]:
                counts[resolved_cache[cwd]] += 1
            continue
        p = Path(cwd).resolve()
        if p == home_resolved:
            resolved_cache[cwd] = None; continue
        root = find_project_root_strict(p)  # returns None if no marker
        resolved_cache[cwd] = str(root) if root else None
        if root: counts[str(root)] += 1
    return counts.most_common(1)[0][0] if counts else None
```

**2. A new `longhand reconcile [--fix]` command.** Walks `~/.claude/projects/*/*.jsonl`, diffs against the `sessions` table, buckets into:

- Fully indexed
- Ingested but `project_id IS NULL`
- Missing from sessions entirely

With `--fix` it re-ingests the problem buckets. Idempotent (upsert + size-check skip). This is the safety net that was missing.

**3. A `stale` flag on `recall_project_status`.** So the next time a caller queries a project with un-ingested transcripts, they see `stale: true` and a reason string pointing at `reconcile --fix` — not silence.

**4. Fixed a pre-existing bug in `discover_sessions`.** It was `rglob`-ing all JSONLs under `~/.claude/projects`, including subagent transcripts (in `*/subagents/` subdirs) and pytest temp dirs. On my machine this was inflating "missing" counts from 28 → 650. The fix is three lines and one regret about not catching it sooner.

Then I ran `longhand reconcile --fix` against my own live DB. **33 sessions re-ingested, 0 errors.** The 2,526-line `823dd358` session got correctly attributed to bsoi-mesh-kit. `recall_project_status` started returning real narrative. 182 tests passing. Tagged v0.6.0, pushed — PyPI Trusted Publishing does the release:

```bash
git push --follow-tags origin main
# ... 45 seconds later ...
pip install longhand==0.6.0  # live
```

## Step 2: audit the fix

I then asked Claude — in the same session — to give me a "full audit full honesty" of what I'd just shipped. **This is the part that matters.**

Claude wrote back a multi-page critique. Some of it was flattering (release pipeline, test discipline). Some of it was not:

> *"The narrative generator leaks garbage into authoritative-looking output. Look at what `recall_project_status("bsoi-mesh-kit")` returned after I fixed everything:*
>
> ```
> Outcome: **fixed** · can you pull my bsoi-ops from my git and review the whole program
>
> Recent commits (10)
> - cc5f72f no message (today)
> - `` no message (today)    ← blank commit hash
> - `` no message (today)
> ... (8 more blanks)
> ```
>
> *The 'fix summary' is pulling a raw user question. The commit list has nine empty entries. Agents will read this as ground truth."*

And another:

> *"Drift detection is 2.3 seconds per `recall_project_status`. On every call, we scan all 59 JSONLs looking for cwd matches. That's going to bite at 500+ sessions."*

Four classes of issue came out of that audit. Four more fixes — all traceable to the audit's specific findings — shipped as v0.7.0 within the same session:

- **Narrative cleanup.** Commits with empty hashes now get dropped at the extractor (no row written), in SQL (filter), AND in the narrative (render-time guard). The "last fix" trailer now sources from the most-recent episode's `fix_summary` instead of the outcome classifier's buggy `summary` field.
- **`longhand doctor` grew a "Recent ingest (7d)" row** that counts on-disk JSONLs in the last week vs sessions-table rows and emits a red ✗ with `reconcile --fix` hint when ratio < 0.5. Catches the next silent-hook-failure the moment the user runs `doctor`.
- **A filesystem-backed drift cache.** `_detect_project_drift` now reads `(transcript_path, mtime) → set[canonical_paths]` from `~/.longhand/cache/jsonl_project_map.json`, keyed on mtime so file edits invalidate automatically. Warm `recall_project_status` on my live DB dropped from **2,333ms → 68ms — 34×**.
- **`search` auto-scopes when the query names a project.** If the query hits a known project at fuzzy-match score ≥0.8 and the caller didn't pass a project filter, the search is pre-scoped to that project's events. The response wraps in `{auto_scoped_to, auto_scope_hint, hits}` so agents can tell the filter applied (and override it if wrong).

`git push --follow-tags` → PyPI → v0.7.0 live. 197 tests passing. 45 seconds.

## The meta point

Two meaningful releases in one session. Both were driven by a failure the tool itself surfaced. Both were audited by the tool itself after shipping. **The tool is its own test harness.**

This is the shape I didn't expect when I started. I thought I was building a memory tool — something that stores and retrieves past work. What I actually ended up with is a **memory tool that can audit its own memory**. When it fails, it fails loudly enough (or I can make it fail loudly enough, on demand) that the failure itself becomes a seed for the next fix.

The industry pitch is "bigger context windows will solve memory." I keep arguing the inverse: the disk already has the memory; you just need a tool that reads it honestly. The last two days have been me testing "reads it honestly" against its own bugs. The tool passed — but only because I forced it to audit itself.

## What's still broken

Since this is a dev.to post and not marketing copy, here's the list of things v0.7.0 doesn't fix. These will probably be v0.8.0:

- **`fix_summary` still looks rough upstream.** The narrative now pulls from `episode.fix_summary` correctly, but that field itself contains raw thinking-block text with "Intent:" prefixes and mid-code truncations. Fix is ~20 lines in the episode extractor.
- **Hook is still a single point of failure.** `doctor` now flags silent failures, but only when the user thinks to run `doctor`. A recall-first user never sees it. Should be inlined into `recall` and `recall_project_status`.
- **Multi-project sessions are winner-takes-all.** A session that spent 51% in project A and 49% in project B attributes only to A. Many-to-many attribution is the right shape; it's not built yet.
- **Auto-scope threshold is a magic `0.8`.** Not calibrated across ambiguous queries yet.
- **22 CLI commands + 16 MCP tools is too many.** Needs a v1.0 prep pass.

## Try it

```bash
pip install longhand==0.7.0
longhand setup         # ingest existing Claude Code history + install hook + register MCP
longhand recall "that bug I fixed last week"
```

If you're already on an older version:

```bash
pip install --upgrade longhand
longhand reconcile --fix   # replay historical sessions with corrected attribution
```

The source is at [github.com/Wynelson94/longhand](https://github.com/Wynelson94/longhand) (MIT). Issues and discussions welcome. If you install it and find a silent failure of your own, please file it — that's the feedback loop that made these two releases happen.

---

*If you built a tool that stores AI session history, how would you test that it's not lying to you? That's the problem Longhand is trying to solve. v0.7.0 is the third time it caught itself; it probably won't be the last.*
