# Longhand Analytics Snapshot — 2026-06-19

> **First snapshot.** No prior `analytics-*.md` exists in the repo. Deltas in §6 are computed against the 2026-05-17 baseline recorded in `outreach/README.md` (~33-day window, not a strict 7-day week). Future snapshots will use true week-over-week deltas.

---

## 1. Live Usage

| Channel | Metric | Value | Notes |
|---------|--------|-------|-------|
| PyPI | Weekly installs | DATA UNAVAILABLE | pypistats.org blocked by network policy |
| PyPI | Last known weekly installs | ~175/wk | 2026-05-17 baseline from outreach/README.md |
| PyPI | Launch peak | ~733/wk | Apr 15–24 spike (from commit message ref) |
| GitHub | Unique visitors (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| GitHub | Unique cloners (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| PulseMCP | Est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com blocked by network policy |
| PulseMCP | Last known weekly visitors | 193 | 2026-04-17 entry in outreach/README.md |

---

## 2. Social Proof

| Metric | Value | Δ vs 2026-05-17 baseline |
|--------|-------|---------------------------|
| GitHub stars | 10 | +2 (+25%) ⚠️ |
| GitHub forks | 4 | +2 (+100%) ⚠️ |
| GitHub watchers | 10 | — (not tracked previously) |
| Open issues | 4 | +4 (was 0) |
| Glama security score | A-tier | ✅ confirmed (glama.json present) |
| Current version | 0.11.1 | — |
| Total releases (since Apr 9 launch) | 8 (v0.6–v0.11.1) | — |

⚠️ = metric moved >20% since prior baseline.

---

## 3. Discovery Channels (GitHub Referrers, 14d)

DATA UNAVAILABLE — GitHub traffic referrer API requires `GET /repos/{owner}/{repo}/traffic/popular/referrers`, which is not exposed by the GitHub MCP tools available in this environment. Add this endpoint or a `gh` CLI call to the routine to recover this data.

**Last known referrer snapshot (2026-05-17):** github.com itself (5 uniques); Facebook (self-seeded by Nate via FB comments — NOT organic virality, do not interpret as viral signal).

---

## 4. Top Paths on Repo

DATA UNAVAILABLE — GitHub traffic path API requires `GET /repos/{owner}/{repo}/traffic/popular/paths`, not exposed by MCP tools.

---

## 5. Distribution Channel Status

Carried forward from `outreach/README.md`. No changes detected this week.

| Channel | Status | Notes |
|---------|--------|-------|
| pulsemcp.com | ✅ LIVE | Auto-ingested via MCP Registry. 193 est. weekly visitors as of 2026-04-17. |
| Claude Code Plugin Marketplace | ✅ LIVE | Published 2026-04-17. Highest-intent channel. |
| glama.ai/mcp | ✅ LIVE | A-tier security/license/quality scores. |
| X/Twitter | ✅ POSTED | 2026-04-17. No new posts this week. |
| Dev.to blog post | ✅ POSTED | 2026-04-17. Stats unavailable (dev.to blocked). |
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

## 6. Since-Baseline Deltas (vs 2026-05-17)

> Note: ~33-day window, not a 7-day week. Future snapshots will be true week-over-week.

| Metric | 2026-05-17 | 2026-06-19 | Δ | Δ% | Flag |
|--------|-----------|-----------|---|-----|------|
| Stars | 8 | 10 | +2 | +25% | ⚠️ >20% |
| Forks | 2 | 4 | +2 | +100% | ⚠️ >20% |
| Open issues | 0 | 4 | +4 | n/a | — |
| Weekly PyPI installs | 175 | DATA UNAVAIL. | — | — | |
| Unique cloners (14d) | 78 | DATA UNAVAIL. | — | — | |
| Unique visitors (14d) | 13 | DATA UNAVAIL. | — | — | |
| Commits this week | 4 (14d) | 7 (Jun 12–18) | +3 | +75% | ⚠️ >20% |

---

## 7. What the Numbers Say

1. **Stars are up 25% and forks doubled since mid-May, with zero new outreach.** Ten stars and 4 forks is still a small-repo number, but the +2/+2 gains happened with no new promotional push — pure residual from the April launch and the plugin marketplace listing. The trajectory is flat-but-not-dead, which is the expected steady-state after a launch spike.

2. **v0.11.1 (Jun 18) closed two silent-corruption bugs affecting real user data.** The project-misattribution fix corrected 13% of sessions being filed under the wrong project on a 265-session real corpus. These are the kind of correctness bugs that erode trust quietly; shipping them fast with a `longhand reattribute` remediation command is the right call before any new outreach. The release cadence this month (v0.9.4 → v0.10.0 → v0.11.0 → v0.11.1 in 10 days) shows the product is actively hardening.

3. **Open issues jumped from 0 to 4.** Without access to the issue list details (the GraphQL list_issues tool returned 0 — likely a filter mismatch vs the REST count of 4), it's unclear whether these are bug reports, feature requests, or stale. Worth a manual triage pass before the next outreach push.

4. **PyPI install rate is unreadable this week.** pypistats.org is blocked by the analytics routine's network policy. The last known figure (175/wk as of 2026-05-17) was already a steep decay from the 733/wk launch peak. Install rate is the most actionable metric for timing the next outreach push — restoring access to pypistats.org in the network egress settings should be the top priority fix for this routine.

5. **GitHub traffic (visitors, cloners, referrers) is also dark.** The GitHub MCP tools don't expose the `/traffic/*` endpoints. Either switch to a `gh` CLI call in the routine, or use a personal-access token that has the `repo` scope against the REST API directly. Without this data, it's impossible to tell if the recent v0.11.x release activity generated any inbound organic traffic.

---

## Data Gaps & Routine Health

| Source | Status | Fix Needed |
|--------|--------|------------|
| pypistats.org | ❌ DATA UNAVAILABLE | Add `pypistats.org` to network egress allowlist |
| dev.to API | ❌ DATA UNAVAILABLE | Add `dev.to` to network egress allowlist |
| pulsemcp.com | ❌ DATA UNAVAILABLE | Add `www.pulsemcp.com` to network egress allowlist |
| GitHub traffic (views/clones/referrers/paths) | ❌ DATA UNAVAILABLE | GitHub MCP tools don't expose `/traffic/*` — use `gh api` calls or direct REST |
| GitHub open issues detail | ⚠️ PARTIAL | REST count = 4; MCP GraphQL returned 0 (filter mismatch). Check MCP tool OPEN state filter. |
