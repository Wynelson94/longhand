# Longhand Analytics Snapshot — 2026-05-29

**Snapshot date:** 2026-05-29 (UTC)  
**Snapshot type:** Bootstrap — first `analytics-*.md` file. No prior snapshot exists; deltas section omitted. Reference baseline from `outreach/README.md` (2026-05-17) is cited inline where useful.

---

## Live Usage

| Metric | Value | Source |
|--------|-------|--------|
| PyPI installs (last day) | DATA UNAVAILABLE | pypistats.org not in network allowlist |
| PyPI installs (last week) | DATA UNAVAILABLE | pypistats.org not in network allowlist |
| PyPI installs (last month) | DATA UNAVAILABLE | pypistats.org not in network allowlist |
| GitHub unique visitors (14d) | DATA UNAVAILABLE | Traffic API requires auth; no `gh` CLI |
| GitHub unique cloners (14d) | DATA UNAVAILABLE | Traffic API requires auth; no `gh` CLI |
| PulseMCP est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com not in network allowlist |
| Current version | v0.9.3 | GitHub commits |
| Tests passing | 269 | v0.9.3 release notes |
| Test coverage | 73% | v0.9.3 CI gate |
| MCP tools exposed | 19 | v0.9.1+ |

> **Prior reference (2026-05-17, from outreach/README.md):** PyPI ~175/wk, unique cloners 78, unique visitors 13.

---

## Social Proof

| Metric | Value | Prior (2026-05-17) | Δ |
|--------|-------|---------------------|---|
| Stars | 10 | 8 | +2 (+25%) |
| Forks | 4 | 2 | +2 (+100%) |
| Watchers/Subscribers | 10 | — | — |
| Open issues | 2 | 0 | +2 (spam — see note) |
| GLAMA tier | A-tier | A-tier (confirmed v0.9.1) | no change |
| SafeSkill score | 93/100 | 93/100 | no change |

> **Open issue note:** Issues #18 and #19 are identical automated spam from user `HMCHENGGH` ("Agent Tool Intel"), opened 2026-05-29 within minutes of the v0.9.3 release merge. Both solicit a "Grade B+" badge. Not organic. The 2 open issues reported by the API are entirely spam.

---

## Discovery Channels — Referrers

DATA UNAVAILABLE — GitHub traffic API requires authentication (`/traffic/popular/referrers`). No `gh` CLI available in this execution environment.

> **Prior reference (2026-05-17):** Top referrer was github.com itself (5 uniques). Facebook-origin traffic is Nate self-seeding via FB comments, not organic virality.

---

## Top Repo Paths

DATA UNAVAILABLE — GitHub traffic API requires authentication (`/traffic/popular/paths`). No `gh` CLI available in this execution environment.

---

## Distribution Channel Status

| Channel | Status | Notes |
|---------|--------|-------|
| pulsemcp.com | ✅ Live | Auto-ingested via Official MCP Registry. 193 weekly visitors as of 2026-04-17; current count unavailable. |
| Claude Code Plugin Marketplace | ✅ Live | Published 2026-04-17. Highest-intent channel. |
| glama.ai/mcp | ✅ Live | A-tier security / license / quality scores. Detailed score data unavailable (network blocked). |
| mcp.so | ⬜ Not submitted | Pending second-push batch. |
| mcpservers.org | ⬜ Not submitted | Pending second-push batch. |
| awesome-mcp-servers | ⬜ Not submitted | Pending second-push batch. |
| awesome-claude-code | ⏸️ Carry-forward blocked | Shipwright #1380 must resolve first; was eligible after Apr 16 but checklist requires zero other open issues. Currently 2 spam issues block eligibility — close them first. |
| Show HN | ⬜ Not posted | Planned for v0.9.2 second push (not yet executed). |
| X/Twitter | ✅ Posted | 2026-04-17 launch thread. |
| Dev.to | ✅ Posted | 2026-04-17. Article stats unavailable (network blocked). |
| Product Hunt | ⬜ Not launched | Awaiting Show HN momentum. |
| TLDR AI | ⬜ Not pitched | Ready to pitch. |
| Ben's Bites | ⬜ Not pitched | Ready to pitch. |
| Latent Space | ⬜ Not pitched | Ready to pitch. |
| The Pragmatic Engineer | ⬜ Not pitched | Ready to pitch. |

---

## What the Numbers Say

1. **V0.9.3 silently fixed two regressions that had been breaking Longhand since v0.9.0.** The `outputSchema` validator incompatibility meant 12 of 19 MCP tools simply didn't load in Claude Code — any user who installed between v0.9.0 and v0.9.2 was running at 7-tool capacity, not 19, without knowing it. The UserPromptSubmit hook was also silently emitting `{}` on every prompt, killing auto-context injection entirely. These are both "the thing didn't work" bugs, not edge-case regressions. Users who installed at launch have been quietly running a degraded experience for weeks.

2. **Stars gained +2 (+25%) in 12 days without a new public push.** Both forks also doubled (2 → 4). At this sample size, individual data points dominate, but the trajectory is not decaying — the repo is attracting quiet organic attention despite no second-push distribution activity having executed since the April launch. The v0.9.3 bug-fix release landed today and may produce a small additional bump as existing users re-engage.

3. **The second-push checklist (Show HN, Tier 1 directory submissions) has not executed yet.** The v0.9.2 plan called for Show HN + mcp.so + mcpservers.org + awesome-mcp-servers in the week of May 17; none of those are checked off. V0.9.3 is now the correct version to pair with that push — the "all 19 tools now work" angle is more credible than leading with the demo command, since the underlying tool actually functions end-to-end.

4. **Spam issues are blocking the awesome-claude-code resubmission checklist.** Issues #18 and #19 must be closed before that submission is eligible (the checklist forbids any open issues). Closing those spam issues is the fastest way to unlock a channel that was already ready to receive a submission.

5. **All PyPI and traffic metrics are unavailable in this snapshot** due to network restrictions in the automated execution environment. The PyPI weekly install rate (last known: ~175/wk as of 2026-05-17, down from 733/wk peak) is the most critical missing signal — it's the primary install-momentum indicator. Future snapshots should be run from an environment with network access to pypistats.org and GitHub's authenticated traffic endpoints.

---

## Data Gaps This Snapshot

| Source | Endpoint | Gap Reason |
|--------|----------|------------|
| PyPI | pypistats.org/api/packages/longhand/recent | Host not in network allowlist |
| GitHub Traffic (views) | /repos/Wynelson94/longhand/traffic/views | Requires auth; no `gh` CLI |
| GitHub Traffic (clones) | /repos/Wynelson94/longhand/traffic/clones | Requires auth; no `gh` CLI |
| GitHub Referrers | /repos/Wynelson94/longhand/traffic/popular/referrers | Requires auth; no `gh` CLI |
| GitHub Top Paths | /repos/Wynelson94/longhand/traffic/popular/paths | Requires auth; no `gh` CLI |
| PulseMCP | pulsemcp.com/servers?q=longhand | Host not in network allowlist |
| Dev.to | dev.to/api/articles/wynelson94/… | Host not in network allowlist |
| Glama | glama.ai/mcp/servers/Wynelson94/longhand | HTTP 403 |
