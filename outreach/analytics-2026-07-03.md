# Longhand Analytics Snapshot — 2026-07-03

> **Week-over-week snapshot.** Prior baseline: `outreach/analytics-2026-06-26.md` (PR #38). True 7-day window: Jun 26 → Jul 3.

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

| Metric | Value | Δ vs 2026-06-26 |
|--------|-------|------------------|
| GitHub stars | 10 | 0 (flat) |
| GitHub forks | 3 | 0 (flat) |
| GitHub watchers | 10 | 0 (flat) |
| Open issues (bugs/features) | 0 | 0 (flat; confirmed via `list_issues` returning 0) |
| Open PRs | 7 | +1 vs 6 prior (all are analytics/maintenance PRs) |
| Glama security score | A-tier | ✅ confirmed (glama.json present in repo) |
| Current version (PyPI) | 0.11.1 | no change |
| GitHub Releases page | v0.9.0 | ⚠️ stale — latest GitHub Release is still v0.9.0 (Apr 29); v0.11.1 not formally released on GitHub |

⚠️ = metric moved >20% vs prior snapshot. No flags this week — all visible metrics flat.

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

## 6. Week-over-Week Deltas (vs 2026-06-26)

| Metric | 2026-06-26 | 2026-07-03 | Δ | Δ% | Flag |
|--------|-----------|-----------|---|-----|------|
| Stars | 10 | 10 | 0 | 0% | — |
| Forks | 3 | 3 | 0 | 0% | — |
| Watchers | 10 | 10 | 0 | 0% | — |
| Open issues (actual) | 0 | 0 | 0 | — | — |
| Open PRs | 6 | 7 | +1 | +17% | — (analytics + maintenance PRs only) |
| Commits to main | 0 (Jun 19–25) | 0 (Jun 26–Jul 2) | 0 | — | — (second consecutive quiet week) |
| GitHub Release (latest tag) | v0.9.0 | v0.9.0 | — | — | ⚠️ now 9 weeks stale vs v0.11.1 on PyPI |
| Weekly PyPI installs | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique cloners (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique visitors (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |

No metrics moved >20% this week.

---

## 7. What the Numbers Say

1. **All GitHub-visible metrics flat for the second consecutive week (stars 10, forks 3, watchers 10).** The April launch residual that produced the +2 stars / +2 forks drift through early June has fully exhausted itself. With no new distribution push and no organic referrer engine running, flat is the expected and stable state until a second push launches. This is not decline — it's baseline.

2. **Seven analytics/maintenance PRs are open and accumulating; the oldest (PR #20, May 29) is now 35 days old with no merge.** The analytics PRs are generating a growing backlog against main. None of the open PRs (analytics #20, #21, #32, #36, #38; doc refresh #37; fieldnotes heal #31) have been merged. If these represent real decisions deferred, the PR list is the right signal. If they represent low-priority housekeeping, a single merge sweep would clean it.

3. **PyPI install data is dark for the fifth consecutive snapshot (May 29 – Jul 3).** The install rate is the primary signal for timing the second outreach push — whether the v0.10.0 secret-redaction and v0.11.x attribution fixes moved the needle is completely unreadable. The ~175/wk figure from May 17 is now 46 days stale. Fixing egress access to `pypistats.org` remains the highest-value fix for this routine.

4. **GitHub Releases page still shows v0.9.0 as the "latest release" — now 9 weeks behind PyPI.** Visitors discovering Longhand via mcp.so, awesome-mcp-servers, or newsletter links see a last-release date of April 29. v0.9.3 through v0.11.1 shipped to PyPI but were never formally released on GitHub. A GitHub Release for v0.11.1 is a low-effort credibility fix that changes the repo's profile card date and surfaces the changelog to anyone checking the releases tab.

5. **Second consecutive zero-commit week on main.** The dense v0.9.4 → v0.10.0 → v0.11.0 → v0.11.1 burst (Jun 9–18) appears to have been followed by a deliberate rest. PR #37 (corpus stats doc refresh, opened Jun 23) is the only pending non-analytics work. The quiet is consistent with post-burst rhythm, not stagnation — but the second push sequence (Show HN, mcp.so, newsletters) has now been deferred for six consecutive weeks with no commit evidence of progress.

---

## 8. Data Gaps & Routine Health

| Source | Status | Fix Needed |
|--------|--------|------------|
| pypistats.org | ❌ DATA UNAVAILABLE (5th consecutive snapshot) | Add `pypistats.org` to network egress allowlist |
| dev.to API | ❌ DATA UNAVAILABLE | Add `dev.to` to network egress allowlist |
| pulsemcp.com | ❌ DATA UNAVAILABLE | Add `www.pulsemcp.com` to network egress allowlist |
| GitHub traffic (views/clones/referrers/paths) | ❌ DATA UNAVAILABLE | GitHub MCP tools don't expose `/traffic/*` — use `gh api` calls or direct REST with `repo`-scoped PAT |
