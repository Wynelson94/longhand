# Longhand Analytics Snapshot — 2026-07-10

> **Week-over-week snapshot.** Prior baseline: `outreach/analytics-2026-07-03.md` (PR #39). True 7-day window: Jul 3 → Jul 10.

---

## 1. Live Usage

| Channel | Metric | Value | Notes |
|---------|--------|-------|-------|
| PyPI | Weekly installs | DATA UNAVAILABLE | pypistats.org blocked by network egress policy (6th consecutive snapshot) |
| PyPI | Last known weekly installs | ~175/wk | 2026-05-17 baseline; PyPI data has been dark since the first snapshot |
| PyPI | Launch peak | ~733/wk | Apr 15–24 spike |
| GitHub | Unique visitors (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| GitHub | Unique cloners (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| PulseMCP | Est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com blocked by network egress policy |
| PulseMCP | Last known weekly visitors | 193 | 2026-04-17 entry in outreach/README.md |

---

## 2. Social Proof

| Metric | Value | Δ vs 2026-07-03 |
|--------|-------|------------------|
| GitHub stars | 11 | **+1 (+10%)** |
| GitHub forks | 3 | 0 (flat) |
| GitHub watchers | 11 | **+1 (+10%)** |
| Open issues (bugs/features) | 1 | **+1** (issue #40 filed Jul 9 — CI py3.14 coverage gap) |
| Open PRs | 6 | -1 vs 7 prior (#37 merged Jul 9; 6 analytics PRs still open but content landed via #42) |
| Glama security score | A-tier | ✅ confirmed (glama.json present in repo) |
| Current version (pyproject.toml) | 0.11.1 | no change (v0.11.2 multiprocess fix merged but not yet tagged/released) |
| GitHub Releases page | v0.9.0 | ⚠️ still stale — now **10+ weeks** behind PyPI (Apr 29 vs Jun 18) |

No metric crossed the >20% flag threshold this week (stars +10%, watchers +10%).

---

## 3. Discovery Channels (GitHub Referrers, 14d)

DATA UNAVAILABLE — `GET /repos/{owner}/{repo}/traffic/popular/referrers` not exposed by the GitHub MCP tools in this environment. Requires `gh api` or direct REST with a token scoped to `repo`.

**Last known referrer snapshot (2026-05-17):** github.com itself (5 uniques); Facebook present but self-seeded by Nate via FB comments — NOT organic virality, do not interpret as a viral signal.

---

## 4. Top Paths on Repo

DATA UNAVAILABLE — `GET /repos/{owner}/{repo}/traffic/popular/paths` not exposed by MCP tools.

---

## 5. Distribution Channel Status

Carried forward from prior snapshot. No channel status changes detected this week.

| Channel | Status | Notes |
|---------|--------|-------|
| pulsemcp.com | ✅ LIVE | Auto-ingested via MCP Registry. 193 est. weekly visitors as of 2026-04-17. |
| Claude Code Plugin Marketplace | ✅ LIVE | Published 2026-04-17. Highest-intent channel. |
| glama.ai/mcp | ✅ LIVE | A-tier security/license/quality scores. |
| X/Twitter | ✅ POSTED | 2026-04-17. No new posts this week. |
| Dev.to blog post | ✅ POSTED | 2026-04-17. Stats unavailable (dev.to blocked by egress policy). |
| mcp.so | ⬜ PENDING | Not yet submitted. |
| mcpservers.org | ⬜ PENDING | Not yet submitted. |
| awesome-mcp-servers | ⬜ PENDING | PR not yet opened. |
| awesome-claude-code | ⏸️ BLOCKED | Was blocked on Shipwright issue #1380; check if unblocked. |
| Show HN | ⬜ PENDING | Not yet posted. |
| Product Hunt | ⬜ PENDING | Scheduled for after HN/newsletter momentum. |
| Medium / Hashnode cross-post | ⬜ PENDING | Awaiting Dev.to spike to justify. |
| TLDR AI | ⬜ PENDING | Newsletter pitch not sent. |
| Ben's Bites | ⬜ PENDING | Newsletter pitch not sent. |
| Latent Space | ⬜ PENDING | Newsletter pitch not sent. |
| The Pragmatic Engineer | ⬜ PENDING | Newsletter pitch not sent. |

---

## 6. Week-over-Week Deltas (vs 2026-07-03)

| Metric | 2026-07-03 | 2026-07-10 | Δ | Δ% | Flag |
|--------|-----------|-----------|---|-----|------|
| Stars | 10 | 11 | +1 | +10% | — |
| Forks | 3 | 3 | 0 | 0% | — |
| Watchers | 10 | 11 | +1 | +10% | — |
| Open real issues (bugs/features) | 0 | 1 | +1 | new | — (first in months; latent CI risk, not active) |
| Open PRs | 7 | 6 | -1 | -14% | — (net: #37/#31/#41 merged; 6 analytics PRs still open but content is in main) |
| Commits to main (week) | 0 | 4 merge commits | — | — | — (ended 2 consecutive zero-commit weeks) |
| GitHub Release (latest tag) | v0.9.0 | v0.9.0 | — | — | ⚠️ now 10+ weeks stale vs v0.11.1 on PyPI |
| PyPI version | 0.11.1 | 0.11.1 | — | — | — (v0.11.2 multiprocess fix merged but not yet tagged) |
| Weekly PyPI installs | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique cloners (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique visitors (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |

---

## 7. What the Numbers Say

1. **Jul 9 was the busiest day on main since the Jun 9–18 burst: 4 PRs merged in one session.** Nate cleared the long-standing analytics backlog via a batch consolidation commit (PR #42 landing all 6 stranded snapshot PRs in one go, with repo auto-merge now enabled so future snapshots self-merge), shipped the v0.11.2 multiprocess reliability overhaul (PR #41), refreshed README corpus stats to 281 sessions / 67 projects / 323 tests (PR #37), and healed fieldnotes pins (PR #31). Two consecutive zero-commit weeks followed by a focused single-day sprint is consistent with Nate's established burst-and-rest rhythm.

2. **Stars ticked from 10 → 11 (+10%), the first star movement in three weeks.** At this scale, single-star movements are noise — but combined with the watchers count also rising 10→11, it's the first positive social signal since the Jun 5 snapshot. The Jul 9 activity (four merged PRs visible in GitHub's public event feed) is the most plausible trigger. No evidence of organic external discovery.

3. **The v0.11.2 multiprocess fix addresses four silent bugs that were present in production since the cli-package refactor.** The dead background spawn (`-m longhand.cli` silently died on every trigger), migration race condition, Stop-hook live-tail guard, and SessionEnd ingest lock were all fixed and each received a regression test. The pyproject.toml still reads 0.11.1 — a version bump, GitHub Release, and PyPI push for 0.11.2 are pending. Visitors checking the releases tab still see v0.9.0 from April 29.

4. **The first open issue in months was filed alongside the merges (issue #40 — CI py3.14 coverage gap).** The 3.14 job runs `continue-on-error: true` due to a SIGILL on onnxruntime manylinux wheels; local 3.14 runs pass (323/323), so the risk is latent. The issue filing marks the first use of the bug tracker for engineering tracking rather than user reports — a sign of pre-v1.0 housekeeping beginning.

5. **Six analytics PRs (#20, #21, #32, #36, #38, #39) remain technically "open" on GitHub even though their content was batch-committed via #42.** This will continue to inflate the open PR count and the GitHub-reported `open_issues_count` (which bundles PRs). Closing these six stale PR branches explicitly (without merging them — since the content is already on main) would clean up the project's public face.

---

## 8. Data Gaps & Routine Health

| Source | Status | Fix Needed |
|--------|--------|------------|
| pypistats.org | ❌ DATA UNAVAILABLE (6th consecutive snapshot) | Add `pypistats.org` to network egress allowlist |
| dev.to API | ❌ DATA UNAVAILABLE | Add `dev.to` to network egress allowlist |
| pulsemcp.com | ❌ DATA UNAVAILABLE | Add `www.pulsemcp.com` to network egress allowlist |
| GitHub traffic (views/clones/referrers/paths) | ❌ DATA UNAVAILABLE | GitHub MCP tools don't expose `/traffic/*` — use `gh api` calls or direct REST with `repo`-scoped PAT |
