# Longhand Distribution Hit List

Track where Longhand has been submitted/posted. Copy for every channel lives in `copy.md`.

## Stable metadata (use everywhere)

- **Name:** Longhand
- **Repo:** https://github.com/Wynelson94/longhand
- **PyPI:** https://pypi.org/project/longhand/
- **Author:** Nate Nelson (nate@blacksheephq.ai)
- **License:** MIT
- **Tagline:** Persistent local memory for Claude Code. Zero API calls, zero summaries, zero AI deciding what matters.
- **Keywords:** `claude-code`, `mcp`, `memory`, `local-first`, `semantic-search`, `sqlite`, `chromadb`, `ai-tools`, `developer-tools`, `cli`

---

## Tier 1 — Fast submissions (30 min total)

These just send qualified traffic with minimal effort.

- [ ] **mcp.so** — https://mcp.so (submit via form or PR to their repo)
- [x] **pulsemcp.com** — ✅ AUTO-INGESTED via Official MCP Registry. 193 weekly visitors as of 2026-04-17. https://www.pulsemcp.com/servers?q=longhand
- [x] **Claude Code Plugin Marketplace** — ✅ PUBLISHED 2026-04-17. Highest-intent channel (users browsing `/plugins` for Claude Code tooling). Likely makes awesome-claude-code listing redundant.
- [x] **glama.ai/mcp** — ✅ ALREADY LISTED with A-tier security/license/quality scores. No claim flow exists; edits happen via repo README. https://glama.ai/mcp/servers/Wynelson94/longhand
- [ ] **mcpservers.org** — https://mcpservers.org (check for submission form)
- [ ] **awesome-mcp-servers** — PR to https://github.com/punkpeye/awesome-mcp-servers
- [ ] **awesome-claude-code** — ⏸️ BLOCKED until Shipwright issue #1380 resolves. Prior Longhand submission #1578 was closed 2026-04-15 (repo too young); eligible to resubmit after Apr 16, but checklist forbids having any other open issue in the repo. Revisit once Shipwright lands. https://github.com/hesreallyhim/awesome-claude-code

→ See `copy.md` §1 for directory descriptions + awesome-list entry text.

## Tier 2 — Content posts (1–2 hours each)

- [ ] **Show HN** — https://news.ycombinator.com/submit (post Tue/Wed 8–10am ET) → `copy.md` §2
- [~] **r/ClaudeAI** — SKIPPED (Nate does not post to Reddit)
- [~] **r/LocalLLaMA** — SKIPPED (Nate does not post to Reddit)
- [x] **X/Twitter thread** — ✅ POSTED 2026-04-17

## Tier 3 — Scheduled / prepared (1–2 weeks out)

- [ ] **Product Hunt launch** — https://www.producthunt.com (schedule for Tue, line up 5–10 supporters)
- [x] **Dev.to blog post** — ✅ POSTED 2026-04-17: https://dev.to/wynelson94/why-i-built-a-lossless-alternative-to-ai-memory-summarization-40cl
- [ ] **Cross-post blog** to Medium + Hashnode — canonicalize back to the dev.to URL above

## Tier 4 — Newsletter pitches (one email each)

- [ ] **TLDR AI** — https://tldr.tech/ai (submit form)
- [ ] **Ben's Bites** — https://bensbites.com (submit form)
- [ ] **Latent Space** — DM @swyx on X with a one-line framing
- [ ] **The Pragmatic Engineer** — Gergely Orosz, case-study framing

## Tier 5 — Do not do (yet)

- ❌ Paid Reddit/X ads (organic is working, wasted budget)
- ❌ r/programming (too broad, 1% signal)
- ❌ Launching HN + PH same day (stagger — HN first, PH 1–2 weeks after)

---

## Sequencing — Second push (paired with v0.9.2)

The April launch produced a real spike (peak 372 installs/day Apr 16) that decayed to ~25/day by mid-May. Second push is paired with the v0.9.2 release for a fresh "what's new since you tried it" angle, plus the SafeSkill 93/100 + GLAMA A-tier badges as legitimacy signals.

**Day 1:** Ship v0.9.2 (release skill handles tag → OIDC publish via GitHub Actions).
**Day 1 (same session):** Tier 1 batch — mcp.so, mcpservers.org, awesome-mcp-servers PR. 30 min total.
**Day 2:** Resubmit awesome-claude-code (was blocked on prior Shipwright issue; should be unblocked).
**Day 2-3:** Show HN at Tue/Wed 8–10am ET (`copy.md` §2). NO Reddit (per Nate's policy).
**Day 4:** X thread (link v0.9.2 changelog + a workflow story, not the launch pitch).
**Week 2:** Newsletter pitches (TLDR AI, Ben's Bites, Latent Space, Pragmatic Engineer). Skip cross-post Medium/Hashnode unless dev.to spikes.
**Week 3–4:** Product Hunt launch IF HN/newsletter gave momentum to leverage.

---

## Current numbers (2026-05-17 — pre-second-push baseline)

For deltas vs the 2026-04-17 launch baseline, see `analytics-2026-04-17.md`.

- **Stars:** 8 (was 4 at launch — +4 in 30 days, mostly post-launch decay tail)
- **Forks:** 2
- **Open issues:** 0
- **Weekly PyPI installs:** 175 (was 733 at peak — **−77%** decay; Apr 15–24 spike fully decayed)
- **Daily PyPI installs (typical):** 5–10, occasional 47–105 bumps
- **Unique GitHub cloners (14d):** 78 (was 336 at launch — **−77%**)
- **Unique GitHub visitors (14d):** 13 (essentially flatlined; no organic referrer engine)
- **Top referrer:** github.com itself (5 uniques). Facebook is self-seeded per `feedback_longhand_fb_seeding` — ignore.
- **Tempo:** 4 commits in last 14d (maintenance mode post-v0.9.1)

## What to watch (post-v0.9.2 push)

- **First 24h after HN:** uniques + cloners spike (target: ≥200 uniques, ≥100 cloners)
- **48–72h after submissions:** Tier 1 referrers showing up (mcp.so + mcpservers.org + awesome-mcp-servers)
- **Week 1 after release:** PyPI weekly installs recovers above 300 (40% of launch peak)
- **Stars:** target +10 in week 1 (proves the angle landed)
- **DMs / issues:** open questions are the real signal — save good ones for FAQ/v0.10.0 priorities
