---
title: I shipped 28 releases teaching my memory tool to stop lying to me
published: false
description: Not crashes. Not data loss. Something quieter — confident answers that weren't true. Here's every one I found on the way to 1.0, with the real numbers.
tags: python, opensource, ai, claude
cover_image:
canonical_url: https://github.com/Wynelson94/longhand
---

*Source of truth for this post is the repo: [github.com/Wynelson94/longhand/blob/main/docs/devto-v1-post.md](https://github.com/Wynelson94/longhand/blob/main/docs/devto-v1-post.md). Edits go through git.*

---

In April I asked my own memory tool where I'd left off on a project I'd worked on that same afternoon.

```
recall_project_status("bsoi-mesh-kit") → "No session history found for this project."
```

There were four transcripts on disk. One of them was 2,526 lines.

I assumed I'd found a bug. What I'd actually found was the first clear look at a bug I'd spend the next four months fixing over and over, in different costumes, across 28 releases.

It never crashed. It never lost data. It just answered confidently, and wrong.

## What the tool is, in one paragraph

[Longhand](https://github.com/Wynelson94/longhand) is a Python CLI and MCP server that reads Claude Code's session transcripts (`~/.claude/projects/**/*.jsonl`), indexes every tool call, file edit, and thinking block into SQLite + ChromaDB, and gives you semantic recall over your whole history. Local-only, zero API calls, nothing summarized. The pitch in one line: *the model doesn't need to carry the memory — the disk does.* `pip install longhand`.

That's the part I set out to build. The rest of this post is the part I didn't plan for.

## Version 0.6: it forgot where the work happened

The 2,526-line session was indexed fine. Every event was in SQLite. The problem was attribution: the tool decided which project a session belonged to by looking at the working directory of the **first event**.

I start most sessions from `$HOME` and `cd` into a project. So the first event's `cwd` was `/Users/natenelson`, and the session got filed under nothing at all — `project_id → NULL`. Invisible to every per-project query.

The fix was to tally the working directories across *all* events, throw out `$HOME` and anything without a project marker, and take the mode. Obvious in hindsight. The interesting part isn't the fix — it's that the tool reported this as **"no session history found"** rather than "I have this session but I don't know where to file it." Those are very different sentences. Only one of them sends you looking in the right place.

## Version 0.11.1: the counters were off by 7.8x

Two months later I noticed my home directory claiming an implausible number of sessions. I checked it against the sessions table.

**2,068 "sessions" against 264 real ones. 53,952 file edits against roughly 7,200 actual.**

The cause was almost embarrassing: `upsert_project()` incremented the counters on *every ingest* of a session. Sessions get ingested more than once — the SessionEnd hook, the live-tail hook's analysis pass, and any `reconcile` re-ingest all touch the same row. The columns weren't counting sessions. They were counting the number of times I'd looked at a session.

The same release fixed a related one: `session.cwd` and `project_id` were written by two independent code paths that could desync, so **35 of 265 sessions — 13% — were filed under the wrong project.**

Raw data was never affected. Search and recall were fine. But every number the tool showed you *about* itself was inflated, and it had been for weeks, because nothing in the system was checking the derived numbers against the source ones.

## Version 0.11.0: it also lied in the other direction

This is the one that changed how I think about the problem.

The tool reported a "resolved rate" — what fraction of the problems it found in your sessions ended in a fix. Mine looked bad. Roughly a third.

The denominator was wrong. It included every low-confidence extraction — probes, tool churn, lines that merely *contained* the word "error." Those aren't problems I failed to solve; they're not problems at all. On my corpus today, with them excluded, the real number is **423 resolved out of 501 substantive episodes — 84%.**

I had spent weeks assuming my resolve rate was mediocre because my own tool told me so, pessimistically, with confidence.

That reframed the whole project for me. I'd been thinking of honesty as a *direction* — don't oversell, be conservative, round down. It isn't. Honest means **accurate**. A tool that understates is lying exactly as much as one that overstates, and it's harder to catch, because understating sounds like humility.

## Versions 0.12 and 0.13: silence is also a lie

Two more of the same species:

**Error counts were inflated by search results.** If you grepped a codebase and the results contained the string `Error:`, the extractor counted that as a problem you'd encountered. It wasn't. It was a search hit. The fix was to make error detection aware of which command produced the output.

**Hooks failed silently.** A hook that dies takes your session's ingest with it, and you find out weeks later when recall comes back thin. 0.13 made every hook failure exit 0 — never break the user's prompt — but leave a breadcrumb on disk and a row in `longhand doctor`. Failing is fine. Failing quietly isn't.

**"Today" wasn't your today.** Recall windows anchored to UTC. If you're not on UTC, asking "what did I do today" silently cut off your own morning.

## Version 1.0: the remedy that couldn't work

By 1.0 I thought I'd learned the lesson. Then I read the row I'd added in 0.13 to surface hook failures:

```
⚠ 23 in the last 7 days — see ~/.longhand/logs/hook-errors-*.log;
  longhand reconcile --fix heals missed ingests
```

Except `reconcile` finds work by walking the **disk**. And 21 of those 23 failures were `missing-transcript` — sessions whose transcript never landed on disk at all. I checked: all 21 files are still absent today. They were never there and never will be.

So the row I'd built specifically to be honest about failures was **recommending a no-op for 21 of the 23 things it was reporting.** It didn't say anything false, exactly. It just confidently pointed you at a command that could not possibly help.

The fix was to split the remedy by failure class. Only one class is actually healable. The rest now say *"these were never written — nothing to heal,"* which is shorter, less helpful-sounding, and true.

## Version 1.0.1: it told me the network was down

Longhand 1.0.0 shipped. Within the hour, dogfooding it on my own corpus turned up three more.

The best one had been sitting in my notes for weeks marked *"unexplained, not blocking."* The `doctor` version row said:

```
Version  ⚠ could not reach pypi.org (offline?)
```

while `curl https://pypi.org/pypi/longhand/json` from the same terminal returned `200`.

It was never the network. On a python.org macOS build, `urllib` verifies certificates against OpenSSL's own trust store rather than the system keychain, so the request fails with `CERTIFICATE_VERIFY_FAILED`. An `except Exception: return None` swallowed the real error, and the message I'd written guessed "offline" — which sent me, repeatedly, to debug a network that was fine.

That guess cost me weeks of not knowing the update check had **never worked on macOS at all.** Nobody on the most common macOS Python setup had ever been told a new version existed.

## Why this class of bug survives so long

None of these produced a stack trace. None crashed. None lost a byte. There's nothing to grep the logs for, because from the program's point of view nothing went wrong.

They survived because **a wrong answer that sounds right doesn't get investigated.** "Offline?" is plausible. "No session history found" is plausible. A 33% resolve rate is plausible — depressing, but plausible. Every one of these passed the sniff test, which is exactly why each one lasted for weeks.

The pattern underneath all of them is the same: *the probe failed, and the program described the world instead of describing the probe.* The certificate check failed → "you're offline." The disk lookup found nothing → "no history exists." Every one of those is a program inferring a cause it has no evidence for.

The defense isn't better logic. It's refusing to answer past your evidence. `pypistats.org is unreachable` is true and useful. `The download data is unavailable` is neither — it's a conclusion about the world drawn from one failed request.

I know that phrasing precisely because I got it wrong again, today, writing this post. Asked how many people were installing Longhand, I checked the pypistats API, got nothing, and reported that the download data was dark. It wasn't. **The PyPI project page had the numbers the whole time.** Same bug, in the research for the post about the bug, four hours after shipping the release that fixed three instances of it.

## So what is 1.0, then

1.0 removed four commands, trimmed the MCP tool list from 19 to 13, flipped one default, and added essentially no features.

That sounds like a strange release until you notice that's what 1.0 is *for*. You cannot promise "nothing will disappear" while still carrying four things you fully intend to delete. So you delete them — after a full release warning people first — and then you make the promise.

The promise is five specific commitments, each with a named enforcement artifact in the repo ([COMPATIBILITY.md](https://github.com/Wynelson94/longhand/blob/main/COMPATIBILITY.md)):

1. **Stable surface** — CLI and MCP frozen through 1.x; removals only at 2.0, and only after warning a full minor ahead.
2. **Forward data compat** — a database written by 0.11+ opens on any later 1.x. There's a real v0.11.2 schema dump in the test suite, generated by executing that tag's own migration code, that proves it.
3. **Hook guarantees** — hooks never raise, never touch the network, never block your prompt.
4. **Upstream drift is never silent** — unknown transcript entries are preserved, surfaced in `doctor`, and regression-gated.
5. **Honest metrics** — counts reflect real signals, and nothing recommends a remedy that can't work.

Promise 5 is the one this entire history paid for. It's the only one I'd have laughed at as a "promise" in April.

A promise without an artifact is a wish, so each one names the test or the guard that fails when it breaks. That's the actual deliverable of 1.0 — not features, but a set of claims someone else can check.

## The numbers, since I'd be a hypocrite otherwise

- **28 releases**, April 15 to August 12. Nine minor lines before 1.0.
- **546 tests.** Python 3.10 through 3.14, all gated in CI.
- **522 downloads last month**, 134 last week.
- **12 GitHub stars.**

That last pair is the interesting one. Twelve stars against several hundred monthly installs — nobody stars a memory tool, they install it and forget it's running. If you're judging your own project by stars, you're reading the wrong instrument.

And the honest version of the trend: **~175/week in May, ~134/week now.** That's a soft decline over three months, during which I did exactly zero distribution work — no Show HN, no newsletter, no posts. It's what a dormant channel looks like, not a verdict on the tool. I'd rather print it than pretend the curve is bending up — which is what I wrote back in April. It was true when I wrote it. I just never went back to check.

## Install

```bash
pip install longhand
longhand setup
```

`setup` backfills your existing Claude Code history, installs the hooks, and registers the MCP server. Safe to re-run.

```bash
longhand recall "that webhook fix from last week"
longhand doctor        # and if it tells you something's wrong, it now means it
```

MIT licensed. Python 3.10+. Zero API calls. Everything stays on your machine.

Repo: [github.com/Wynelson94/longhand](https://github.com/Wynelson94/longhand)

---

*If there's one thing worth stealing from four months of this: go read the error messages you wrote when you were tired. Not the logic — the messages. Find every place where your code guesses a cause instead of reporting what it observed. That's where mine were hiding, all 28 releases of them.*
