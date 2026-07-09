# Longhand Analytics Snapshot — 2026-06-26

> **Week-over-week snapshot.** Prior baseline: `outreach/analytics-2026-06-19.md` (PR #36). True 7-day window: Jun 19 → Jun 26.

---

## 1. Live Usage

| Channel | Metric | Value | Notes |
|---------|--------|-------|-------|
| PyPI | Weekly installs | DATA UNAVAILABLE | pypistats.org blocked by network egress policy |
| PyPI | Last known weekly installs | ~175/wk | 2026-05-17 baseline; PyPI data has been dark since first snapshot (May 29) |
| PyPI | Launch peak | ~733/wk | Apr 15–24 spike |
| GitHub | Unique visitors (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| GitHub | Unique cloners (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| PulseMCP | Est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com blocked by network egress policy |
| PulseMCP | Last known weekly visitors | 193 | 2026-04-17 entry in outreach/README.md |

---

## 2. Social Proof

| Metric | Value | Δ vs 2026-06-19 |
|--------|-------|------------------|
| GitHub stars | 10 | 0 (flat) |
| GitHub forks | 3 | −1 (−25%) ⚠️ |
| GitHub watchers | 10 | 0 (flat) |
| Open issues (bugs/features) | 0 | 0 (flat; prior "4" was open PRs misread from REST count) |
| Open PRs | 6 | +2 vs ~4 prior |
| Glama security score | A-tier | ✅ confirmed (glama.json present in repo) |
| Current version (PyPI) | 0.11.1 | no change |
| GitHub Releases page | v0.9.0 | ⚠️ stale — latest GitHub Release is still v0.9.0 (Apr 29); v0.11.1 not formally released on GitHub |

⚠️ = metric moved >20% vs prior snapshot.

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

## 6. Week-over-Week Deltas (vs 2026-06-19)

| Metric | 2026-06-19 | 2026-06-26 | Δ | Δ% | Flag |
|--------|-----------|-----------|---|-----|------|
| Stars | 10 | 10 | 0 | 0% | — |
| Forks | 4 | 3 | −1 | −25% | ⚠️ >20% |
| Watchers | 10 | 10 | 0 | 0% | — |
| Open issues (actual) | 0* | 0 | 0 | — | — |
| Open PRs | ~4 | 6 | +2 | +50% | — (analytics + maintenance PRs, not organic) |
| Commits to main | 7 (Jun 12–18) | 0 (Jun 19–25) | −7 | −100% | — (burst cadence, not decline) |
| GitHub Release (latest tag) | v0.9.0 | v0.9.0 | — | — | ⚠️ stale vs v0.11.1 on PyPI |
| Weekly PyPI installs | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique cloners (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique visitors (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |

*The prior snapshot's REST `open_issues_count: 4` included open PRs; actual bug/feature issues were 0. Now `open_issues_count: 6` reflects 6 open PRs. Confirmed via MCP `list_issues` returning 0 open issues.

---

## 7. What the Numbers Say

1. **One fork was deleted this week (4 → 3, −25%).** On a 10-star repo, a single fork removal is within noise — no outreach event triggered a mass unforking, and no new activity explains new interest. Most likely a GitHub profile cleanup. Not a meaningful signal.

2. **Stars and watchers were flat at 10.** The previous ~33-day window showed quiet organic growth (+2 stars, +2 forks) from post-launch residual and the plugin marketplace listing. This week shows zero movement on stars. With the April launch fully decayed and no new distribution push, flat is the expected trajectory. The April launch spike is now 10 weeks behind; without a second push, this number will remain stationary.

3. **Zero commits to main since Jun 18 (v0.11.1 merge).** The project is in a post-release breathing window after a dense burst week (v0.9.4 → v0.10.0 → v0.11.0 → v0.11.1, all merged Jun 9–18). PR #37 (corpus stats doc refresh, opened Jun 23) is the only pending work. This quiet is normal and expected.

4. **GitHub Releases page still shows v0.9.0 as the "latest release" — now 8 weeks stale.** Any visitor landing on the repo via discovery (mcp.so, awesome-mcp-servers, newsletter) sees a project last formally released April 29. v0.9.3 through v0.11.1 are on PyPI but not in GitHub Releases. Before the next outreach push, creating a GitHub Release for v0.11.1 is a quick credibility fix: it changes the profile card date and surfaces the changelog to visitors who judge freshness by the releases tab.

5. **PyPI install data has been dark for four consecutive snapshots (May 29 – Jun 26).** This is the single most actionable metric for timing the second outreach push — whether install rate is recovering, flatlining at ~25/day, or already below noise. The egress policy blocking `pypistats.org` is a persistent blind spot. Until this is resolved, the analytics routine cannot tell Nate whether the hardening releases (v0.10.0, v0.11.x) moved the install needle at all.

---

## 8. Data Gaps & Routine Health

| Source | Status | Fix Needed |
|--------|--------|------------|
| pypistats.org | ❌ DATA UNAVAILABLE | Add `pypistats.org` to network egress allowlist |
| dev.to API | ❌ DATA UNAVAILABLE | Add `dev.to` to network egress allowlist |
| pulsemcp.com | ❌ DATA UNAVAILABLE | Add `www.pulsemcp.com` to network egress allowlist |
| GitHub traffic (views/clones/referrers/paths) | ❌ DATA UNAVAILABLE | GitHub MCP tools don't expose `/traffic/*` — use `gh api` calls or direct REST with `repo`-scoped PAT |
