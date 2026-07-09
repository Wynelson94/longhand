# Longhand Analytics Snapshot — 2026-06-05

**Snapshot date:** 2026-06-05  
**Previous snapshot:** None (first snapshot — no prior `outreach/analytics-*.md` file found)  
**Baseline for deltas:** `outreach/README.md` § "Current numbers (2026-05-17 — pre-second-push baseline)"

---

## Live Usage

| Metric | Value | Source | Notes |
|---|---|---|---|
| PyPI weekly installs | DATA UNAVAILABLE | pypistats.org | 403 — outbound network blocked in this execution environment |
| PyPI monthly installs | DATA UNAVAILABLE | pypistats.org | 403 — outbound network blocked |
| GitHub stars | 10 | GitHub API | |
| GitHub forks | 4 | GitHub API | |
| GitHub watchers | 10 | GitHub API | |
| PulseMCP est. weekly visitors | DATA UNAVAILABLE | pulsemcp.com | 403 — outbound network blocked |

---

## Social Proof

| Signal | Value | Notes |
|---|---|---|
| GitHub stars | 10 | |
| GitHub forks | 4 | |
| Glama.ai tier | A-tier (last confirmed pre-2026-05-17) | `glama.json` present in repo; no score embedded in file |
| SafeSkill score | 93/100 | Badge added 2026-05-07 via PR #7 |

---

## Discovery Channels (Referrers)

| Referrer | Uniques (14d) | Notes |
|---|---|---|
| DATA UNAVAILABLE | — | GitHub traffic API not exposed via available MCP tools; requires `gh` CLI or a REST token with `repo` scope |

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
| mcp.so | ⏸️ Pending | Not yet submitted |
| mcpservers.org | ⏸️ Pending | Not yet submitted |
| awesome-mcp-servers | ⏸️ Pending | PR not yet opened |
| awesome-claude-code | ⏸️ Blocked | Prior submission #1578 closed 2026-04-15 (repo too young); eligible to resubmit once Shipwright issue #1380 resolves |
| Show HN | ⏸️ Planned | Second-push sequence drafted in outreach/README.md but no commit evidence it has launched |
| Product Hunt | ⏸️ Planned | Awaiting HN/newsletter momentum per sequencing plan |
| TLDR AI / Ben's Bites / Latent Space / Pragmatic Engineer | ⏸️ Planned | Newsletter pitches not yet sent |

---

## Week-over-Week Deltas

> **Baseline:** `outreach/README.md` "Current numbers" as of 2026-05-17.  
> **Current:** GitHub API via MCP, pulled 2026-06-05.  
> ⚠️ = metric moved >20% in either direction.

| Metric | 2026-05-17 baseline | 2026-06-05 | Δ% | Flag |
|---|---|---|---|---|
| GitHub stars | 8 | 10 | +25% | ⚠️ |
| GitHub forks | 2 | 4 | +100% | ⚠️ |
| Open issues | 0 | 0 | 0% | |
| PyPI weekly installs | 175 | DATA UNAVAILABLE | — | |
| GitHub unique cloners (14d) | 78 | DATA UNAVAILABLE | — | |
| GitHub unique visitors (14d) | 13 | DATA UNAVAILABLE | — | |
| PulseMCP weekly visitors | 193 (as of 2026-04-17) | DATA UNAVAILABLE | — | |
| Dev.to page views | DATA UNAVAILABLE | DATA UNAVAILABLE | — | |

---

## What the Numbers Say

1. **Forks doubled, stars up 25% — v0.9.3 is the likely driver.** In the ~19 days since the 2026-05-17 baseline, forks went 2→4 and stars 8→10. This window contains the v0.9.3 release (2026-05-29), which fixed two bugs that had been silently breaking core functionality since v0.9.0: the UserPromptSubmit hook was emitting `{}` on every prompt, and 12 of 19 MCP tools weren't loading in Claude Code at all due to an `outputSchema` validator mismatch. Users hitting real breakage and then seeing it fixed is the most plausible driver of both metrics.

2. **v0.9.3 repaired a severely degraded baseline experience.** Anyone who installed between v0.9.0 and v0.9.3 was running Longhand with most tools silently unavailable and auto-context injection dead. The fix is material: next outreach should lead with "if you tried it before June and it seemed broken, it was — here's what v0.9.3 fixed" rather than a fresh-install pitch.

3. **Second-push distribution has not launched yet.** The sequencing plan in `outreach/README.md` (Show HN, mcp.so, mcpservers.org, awesome-mcp-servers PR, newsletter pitches) was drafted in May alongside v0.9.2 but there is no commit evidence in the git log that any of it has been executed. v0.9.3 strengthens the story — the "12 of 19 tools now actually work" angle is concrete and verifiable.

4. **Most traffic-level metrics are unavailable this week.** PyPI download counts, GitHub clone/view traffic, PulseMCP visitor counts, and Dev.to article stats all returned HTTP 403 from this execution environment (outbound network policy blocks these external hosts). The next scheduled snapshot should verify outbound access to these endpoints before running; without install velocity data, growth signals are limited to repository metadata.

5. **Facebook referrer traffic remains self-seeded — not a virality signal.** The May baseline noted FB as the top referrer source. This is Nate seeding via FB comments, not organic discovery. It should not be interpreted as a super-spreader event or counted toward channel performance.

---

*Generated: 2026-06-05 by Longhand weekly analytics agent.*  
*Data sources: GitHub MCP (stars, forks, issues, commits) · outreach/README.md (2026-05-17 baseline). All other sources returned DATA UNAVAILABLE due to outbound network restrictions.*
