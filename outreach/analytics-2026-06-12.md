# Longhand Analytics Snapshot — 2026-06-12

**Snapshot date:** 2026-06-12  
**Previous snapshot:** `outreach/analytics-2026-06-05.md` (PR #21, branch `analytics/2026-06-05`)  
**Baseline for WoW deltas:** 2026-06-05 snapshot values

---

## Live Usage

| Metric | Value | Source | Notes |
|---|---|---|---|
| PyPI weekly installs | DATA UNAVAILABLE | pypistats.org | 403 — outbound network blocked in this execution environment |
| PyPI monthly installs | DATA UNAVAILABLE | pypistats.org | 403 — outbound network blocked |
| GitHub stars | 10 | GitHub API | Flat vs prior snapshot |
| GitHub forks | 4 | GitHub API | Flat vs prior snapshot |
| GitHub watchers | 10 | GitHub API | Flat vs prior snapshot |
| PulseMCP est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com | 403 — outbound network blocked |

---

## Social Proof

| Signal | Value | Notes |
|---|---|---|
| GitHub stars | 10 | Flat since 2026-06-05 |
| GitHub forks | 4 | Flat since 2026-06-05 |
| Glama.ai tier | A-tier (last confirmed pre-2026-05-17) | `glama.json` present in repo; no score embedded in file |
| SafeSkill score | 93/100 | Badge added 2026-05-07 via PR #7 |
| Latest version (commit) | v0.11.0 | Released 2026-06-09 via merge commit; PyPI should reflect this |
| Latest GitHub Release | v0.9.0 (2026-04-29) | GitHub releases page not updated since v0.9.0; newer versions ship via PyPI/commits only |

---

## Discovery Channels (Referrers)

| Referrer | Uniques (14d) | Notes |
|---|---|---|
| DATA UNAVAILABLE | — | GitHub traffic API not exposed via available MCP tools; requires `gh` CLI or REST token with `repo` scope |

---

## Top Paths on Repo

| Path | Views (14d) | Notes |
|---|---|---|
| DATA UNAVAILABLE | — | GitHub traffic API not exposed via available MCP tools |

---

## Distribution Channel Status

| Channel | Status | Notes |
|---|---|---|
| pulsemcp.com | ✅ Listed | Auto-ingested via Official MCP Registry; 193 weekly visitors as of 2026-04-17 (current count unavailable — 403) |
| Claude Code Plugin Marketplace | ✅ Published | Published 2026-04-17; highest-intent channel |
| glama.ai/mcp | ✅ Listed | A-tier security / license / quality scores |
| X/Twitter | ✅ Posted | Thread posted 2026-04-17 |
| Dev.to blog post | ✅ Posted | Posted 2026-04-17; article stats unavailable — 403 |
| mcp.so | ⏸️ Pending | Not yet submitted; no commit evidence |
| mcpservers.org | ⏸️ Pending | Not yet submitted; no commit evidence |
| awesome-mcp-servers | ⏸️ Pending | PR not yet opened; no commit evidence |
| awesome-claude-code | ⏸️ Blocked | Prior submission #1578 closed 2026-04-15; resubmit pending Shipwright issue #1380 resolution |
| Show HN | ⏸️ Planned | Second-push sequence drafted in outreach/README.md; no commit evidence of launch |
| Product Hunt | ⏸️ Planned | Awaiting HN/newsletter momentum per sequencing plan |
| TLDR AI / Ben's Bites / Latent Space / Pragmatic Engineer | ⏸️ Planned | Newsletter pitches not yet sent; no commit evidence |

---

## Week-over-Week Deltas

> **Prior week:** `outreach/analytics-2026-06-05.md` snapshot pulled 2026-06-05.  
> **Current:** GitHub API via MCP, pulled 2026-06-12.  
> ⚠️ = metric moved >20% in either direction.  
> Note: all traffic-level metrics (PyPI, clones, views, PulseMCP) are DATA UNAVAILABLE in both periods due to network restrictions — delta cannot be computed.

| Metric | 2026-06-05 | 2026-06-12 | Δ% | Flag |
|---|---|---|---|---|
| GitHub stars | 10 | 10 | 0% | |
| GitHub forks | 4 | 4 | 0% | |
| GitHub watchers | 10 | 10 | 0% | |
| Open issues | 0 | 0 | 0% | |
| Open PRs | 1 (PR #21) | 3 (PRs #20, #21, #31) | +200% | ⚠️ new metric |
| Releases shipped (week) | 1 (v0.9.3, 2026-05-29) | 3 (v0.9.4, v0.10.0, v0.11.0 — all 2026-06-09) | — | ⚠️ release burst |
| Test count | 280 (v0.9.4 baseline) | 316 (v0.11.0) | +13% | |
| PyPI weekly installs | DATA UNAVAILABLE | DATA UNAVAILABLE | — | |
| GitHub unique cloners (14d) | DATA UNAVAILABLE | DATA UNAVAILABLE | — | |
| GitHub unique visitors (14d) | DATA UNAVAILABLE | DATA UNAVAILABLE | — | |
| PulseMCP weekly visitors | DATA UNAVAILABLE | DATA UNAVAILABLE | — | |
| Dev.to page views | DATA UNAVAILABLE | DATA UNAVAILABLE | — | |

---

## What the Numbers Say

1. **Three releases in one day — the quality sprint landed June 9.** v0.9.4 (security hardening, closing 6 AUDIT-2026-05-28 findings), v0.10.0 (opt-in secret redaction across 13 secret patterns, plus chromadb `<1.0` pin and mypy now blocking CI), and v0.11.0 (grouped CLI help panels, hidden plumbing commands, honest episode stats with a low-confidence bucket, data-derived first-run suggestions) all merged the same day. 280 → 316 tests. This is the strongest technical week since the April launch, but install-velocity data (PyPI) is still dark — no way to confirm whether it moved the needle.

2. **GitHub-visible engagement held flat.** Stars (10) and forks (4) are unchanged from the June 5 snapshot. Flat is not bad given no new public distribution push this week, but it confirms the April spike has fully decayed and the project is in maintenance-mode discovery. The v0.9.4/v0.10.0/v0.11.0 quality story needs distribution to turn into stars.

3. **GitHub releases page still shows v0.9.0 as latest.** v0.9.3, v0.9.4, v0.10.0, and v0.11.0 shipped via merge commits and PyPI but no GitHub release was published for any of them. This matters for discovery: the releases tab is often the first thing a visitor checks to gauge project health. A visitor landing on the repo sees "latest release: v0.9.0, April 2026" while the actual latest is v0.11.0, June 2026 — a six-week staleness signal that undersells the cadence.

4. **Three open PRs, zero open issues.** PR #31 (fieldnotes drift gate) opened 2026-06-11 shows active development one day after the release burst. PRs #20 and #21 are prior analytics snapshots that haven't been merged. Issues queue is empty — no user-reported bugs or feature requests visible.

5. **Second-push distribution sequence remains unlaunched.** The Show HN post, mcp.so submission, mcpservers.org, awesome-mcp-servers PR, and all newsletter pitches drafted in `outreach/README.md` show no commit evidence of execution in three consecutive weekly snapshots. The v0.11.0 UX improvements and v0.10.0 secret redaction give fresh angles. The "if you tried it before June and most tools were broken, v0.9.3 fixed that" message is now a month stale and will need refreshing for a second push.

---

*Generated: 2026-06-12 by Longhand weekly analytics agent.*  
*Data sources: GitHub MCP (stars, forks, watchers, issues, PRs, commits, releases) · `outreach/analytics-2026-06-05.md` (prior week baseline). All other sources returned DATA UNAVAILABLE due to outbound network restrictions (pypistats.org, pulsemcp.com, dev.to, GitHub traffic REST API).*
