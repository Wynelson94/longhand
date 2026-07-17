# Longhand Analytics Snapshot — 2026-07-17

> **Week-over-week snapshot.** Prior baseline: `outreach/analytics-2026-07-10.md` (PR #43). True 7-day window: Jul 10 → Jul 17.

---

## 1. Live Usage

| Channel | Metric | Value | Notes |
|---------|--------|-------|-------|
| PyPI | Weekly installs | DATA UNAVAILABLE | pypistats.org blocked by network egress policy (7th consecutive snapshot) |
| PyPI | Last known weekly installs | ~175/wk | 2026-05-17 baseline; PyPI data has been dark since the first snapshot |
| PyPI | Launch peak | ~733/wk | Apr 15–24 spike |
| GitHub | Unique visitors (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| GitHub | Unique cloners (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| PulseMCP | Est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com blocked by network egress policy |
| PulseMCP | Last known weekly visitors | 193 | 2026-04-17 entry in outreach/README.md |

---

## 2. Social Proof

| Metric | Value | Δ vs 2026-07-10 |
|--------|-------|------------------|
| GitHub stars | 12 | **+1 (+9%)** |
| GitHub forks | 3 | 0 (flat) |
| GitHub watchers | 12 | **+1 (+9%)** |
| Open issues (bugs/features) | 1 | 0 (issue #40 still open — CI py3.14 coverage gap) |
| Open PRs | 0 | **-6 (-100%)** ⚠️ FLAG — all 6 stale analytics PRs explicitly closed |
| Glama security score | A-tier | ✅ confirmed (glama.json present in repo) |
| Current version (pyproject.toml) | 0.13.0 | **+2 minor versions** (0.11.1 → 0.12.0 → 0.13.0, both shipped this week) |
| GitHub Releases page | v0.13.0 | ✅ **resolved** — was stale at v0.9.0 for 10+ weeks; v0.11.0 through v0.13.0 all published Jul 11 |
| Test suite size | 524 | **+201 (+62%)** ⚠️ FLAG (was 323) |

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

## 6. Week-over-Week Deltas (vs 2026-07-10)

| Metric | 2026-07-10 | 2026-07-17 | Δ | Δ% | Flag |
|--------|-----------|-----------|---|-----|------|
| Stars | 11 | 12 | +1 | +9% | — |
| Forks | 3 | 3 | 0 | 0% | — |
| Watchers | 11 | 12 | +1 | +9% | — |
| Open real issues (bugs/features) | 1 | 1 | 0 | 0% | — (issue #40 unchanged) |
| Open PRs | 6 | 0 | -6 | -100% | ⚠️ FLAG — 6 stale analytics PRs explicitly closed |
| Commits to main (week) | 4 merge commits | ~26 commits (14 PRs merged) | — | — | ⚠️ most active week since launch |
| GitHub Release (latest tag) | v0.9.0 | v0.13.0 | — | — | ✅ 10-week staleness fully resolved |
| PyPI version | 0.11.1 | 0.13.0 | +2 minor | — | — (v0.12.0 Jul 10, v0.13.0 Jul 11) |
| Test suite size | 323 | 524 | +201 | +62% | ⚠️ FLAG |
| MCP surface (active tools) | 19 | 13 | -6 | -32% | ✅ consolidation (deprecated names still answer) |
| Corpus sessions | 281 | 339 | +58 | +21% | ⚠️ FLAG (from README refresh; reflects real growth) |
| Corpus projects | 67 | 35 | -32 | -48% | ✅ attribution overhaul collapsed junk projects |
| Weekly PyPI installs | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique cloners (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique visitors (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |

---

## 7. What the Numbers Say

1. **Jul 11 was the biggest single-day commit sprint in project history — 14 PRs merged, two versions shipped, and all GitHub releases backfilled in one sitting.** v0.12.0 (update channel, attribution overhaul, honest episodes, storage hygiene — 7 PRs) merged Jul 10 as the Tier 2 series. v0.13.0 (never-crash hooks, honest errors, local-day recall, Windows CI, MCP/CLI consolidation — 10 more PRs) followed the next morning. The GitHub Releases page went from 10+ weeks stale (v0.9.0) to fully current (v0.11.0 through v0.13.0) in one batch publish. This was Nate's most productive week since launch.

2. **The test suite jumped from 323 → 524 (+62%), crossing the >20% flag threshold.** This is engineering velocity, not noise: the hook-guarantee suite (PR #59) enforces the three never-raise / never-network / never-block-inline invariants for all three hooks with 13 injected-failure cases; the transcript-shapes fixture gate (PR #63) fails the build when any entry type lacks a disposition; and the full v0.12.0 and v0.13.0 series added direct tests for previously untested modules. A 62% test expansion in one week signals that v0.13.0 is a hardening release in the true sense — the guarantees are now CI-enforced, not just asserted in prose.

3. **The 6 stale analytics PRs (#20, #21, #32, #36, #38, #39) are now closed — GitHub shows 0 open PRs.** Last week's snapshot flagged these as inflating the public PR count and noted explicit closure would clean up the project's face. That cleanup happened. A visitor landing on the repo today sees a clean PR queue: no stale branches, no confusing "open" PRs whose content is already on main. Combined with the GitHub Release backfill, the repo's public presentation is now consistent with its actual state.

4. **The v0.13.0 deprecation table signals that v1.0 planning is real.** Six CLI commands and six MCP tools now carry explicit deprecation notices, with documented 1:1 replacements and a stated removal point (v1.0). The CLAUDE.md decision tree was rewritten around the 13 surviving MCP tools. For existing users with saved instructions referencing the old names (`search_in_context`, `get_latest_events`, `match_project`, etc.), the deprecated tools still answer with a migration preamble — the transition is opt-in and non-breaking through 0.x. For new users, the surface is cleaner.

5. **The Windows liveness-probe bug fix (PR #57) was a silent data-loss risk on every Windows install shipped since the cli-package refactor.** `os.kill(pid, 0)` on Windows calls `TerminateProcess` rather than probing existence — the ingest-lock "liveness check" was terminating live lock holders, meaning any Windows user with two concurrent sessions or a running MCP server was having processes silently killed. The Windows CI leg added in the same week (PR #66) is non-blocking and evidence-gathering, but its first two runs were green — the first concrete cross-platform CI evidence the project has had.

---

## 8. Data Gaps & Routine Health

| Source | Status | Fix Needed |
|--------|--------|------------|
| pypistats.org | ❌ DATA UNAVAILABLE (7th consecutive snapshot) | Add `pypistats.org` to network egress allowlist |
| dev.to API | ❌ DATA UNAVAILABLE | Add `dev.to` to network egress allowlist |
| pulsemcp.com | ❌ DATA UNAVAILABLE | Add `www.pulsemcp.com` to network egress allowlist |
| GitHub traffic (views/clones/referrers/paths) | ❌ DATA UNAVAILABLE | GitHub MCP tools don't expose `/traffic/*` — use `gh api` calls or direct REST with `repo`-scoped PAT |
