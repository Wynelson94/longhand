# Longhand Analytics Snapshot — 2026-07-24

> **Week-over-week snapshot.** Prior baseline: `outreach/analytics-2026-07-17.md` (PR #70). True 7-day window: Jul 17 → Jul 24.

---

## 1. Live Usage

| Channel | Metric | Value | Notes |
|---------|--------|-------|-------|
| PyPI | Weekly installs | DATA UNAVAILABLE | pypistats.org blocked by network egress policy (8th consecutive snapshot) |
| PyPI | Last known weekly installs | ~175/wk | 2026-05-17 baseline; PyPI data has been dark since the first snapshot |
| PyPI | Launch peak | ~733/wk | Apr 15–24 spike |
| GitHub | Unique visitors (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| GitHub | Unique cloners (14d) | DATA UNAVAILABLE | GitHub traffic API not available via MCP tools |
| PulseMCP | Est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com blocked by network egress policy |
| PulseMCP | Last known weekly visitors | 193 | 2026-04-17 entry in outreach/README.md |

---

## 2. Social Proof

| Metric | Value | Δ vs 2026-07-17 |
|--------|-------|------------------|
| GitHub stars | 12 | 0 (flat) |
| GitHub forks | 3 | 0 (flat) |
| GitHub watchers | 12 | 0 (flat) |
| Open issues (bugs/features) | 1 | 0 (issue #40 still open — CI py3.14 coverage gap) |
| Open PRs | 0 | 0 (flat) |
| Glama security score | A-tier | ✅ confirmed (glama.json present in repo) |
| Current version (pyproject.toml) | 0.13.0 | 0 (flat — no release this week) |
| GitHub Releases page | v0.13.0 | 0 (flat) |
| Test suite size | 524 | 0 (flat — no code changes this week) |

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

## 6. Week-over-Week Deltas (vs 2026-07-17)

| Metric | 2026-07-17 | 2026-07-24 | Δ | Δ% | Flag |
|--------|-----------|-----------|---|-----|------|
| Stars | 12 | 12 | 0 | 0% | — |
| Forks | 3 | 3 | 0 | 0% | — |
| Watchers | 12 | 12 | 0 | 0% | — |
| Open issues (bugs/features) | 1 | 1 | 0 | 0% | — (issue #40 unchanged) |
| Open PRs | 0 | 0 | 0 | 0% | — |
| Commits to main (week) | ~26 commits (14 PRs merged) | 2 commits (analytics PR only) | -24 | -92% | ⚠️ FLAG — but expected: post-sprint cooldown |
| GitHub Release (latest tag) | v0.13.0 | v0.13.0 | 0 | 0% | — |
| PyPI version | 0.13.0 | 0.13.0 | 0 | 0% | — |
| Test suite size | 524 | 524 | 0 | 0% | — |
| Weekly PyPI installs | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique cloners (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |
| Unique visitors (14d) | DATA UNAVAIL. | DATA UNAVAIL. | — | — | |

---

## 7. What the Numbers Say

1. **This was the quietest week since launch by commit volume — two commits, both the analytics PR.** After the Jul 11 sprint (26+ commits, 14 PRs merged, two minor versions shipped), the week of Jul 17–24 produced zero code changes. This is expected recovery, not stagnation: a week that concentrated ~10 weeks of backlog into a single sprint earns a rest. The project is healthy — CI is green, no open PRs other than analytics, no regressions filed.

2. **Stars, forks, and watchers are flat at 12/3/12 — three weeks at this level.** The social proof numbers have held steady since the Jul 10 snapshot. This is consistent with the lack of new discovery channel activation: no Show HN, no Product Hunt launch, no newsletter pitches sent. The existing channels (pulsemcp.com, Claude Code Plugin Marketplace, glama.ai) are doing quiet maintenance work. Growth resumes when a new distribution push happens.

3. **Issue #40 (Python 3.14 CI gap) remains the only open issue.** It's been open since 2026-07-09 and is a known latent risk, not an active regression. The plan documented in the issue is clear: annotation step first, then drop `continue-on-error` once onnxruntime ships working cp314 wheels. v1.0 hard-gates 3.14. No new issues filed this week signals a stable release.

4. **v0.13.0 is holding as the production version — no patch release needed.** In weeks where a hardening release like v0.13.0 is fresh, the absence of any follow-up bugfix PR is a positive signal. The never-crash hooks, Windows liveness-probe fix, and the CI-gated guarantee suite appear to be holding in the field.

---

## 8. Data Gaps & Routine Health

| Source | Status | Fix Needed |
|--------|--------|------------|
| pypistats.org | ❌ DATA UNAVAILABLE (8th consecutive snapshot) | Add `pypistats.org` to network egress allowlist |
| dev.to API | ❌ DATA UNAVAILABLE | Add `dev.to` to network egress allowlist |
| pulsemcp.com | ❌ DATA UNAVAILABLE | Add `www.pulsemcp.com` to network egress allowlist |
| GitHub traffic (views/clones/referrers/paths) | ❌ DATA UNAVAILABLE | GitHub MCP tools don't expose `/traffic/*` — use `gh api` calls or direct REST with `repo`-scoped PAT |
