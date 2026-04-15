# Distribution submissions — ready-to-paste artifacts

Pre-drafted copy for the three upstream registries/lists. Work through in order.

---

## D2 — Official MCP Registry (`registry.modelcontextprotocol.io`)

The registry is submitted to via a CLI + GitHub auth, not a PR. Prerequisites: a new PyPI release (v0.5.5) so the `<!-- mcp-name: ... -->` marker in the README is present in the description PyPI serves back.

**Prereq: ship v0.5.5 to PyPI**

```bash
cd ~/Projects/longhand
git add -A
git commit -m "v0.5.5: plugin manifest + MCP registry manifest"
git push origin main
git tag -a v0.5.5 -m "v0.5.5: Claude Code plugin + MCP registry submission"
git push origin v0.5.5
# Wait for GitHub Actions workflow to publish to PyPI.
# Verify:
curl -s https://pypi.org/pypi/longhand/json | jq .info.version
# Should print: "0.5.5"
```

**Then publish to the registry:**

```bash
# Install the publisher CLI (one-time)
curl -L "https://github.com/modelcontextprotocol/registry/releases/latest/download/mcp-publisher_$(uname -s | tr '[:upper:]' '[:lower:]')_$(uname -m | sed 's/x86_64/amd64/;s/aarch64/arm64/').tar.gz" \
  | tar xz mcp-publisher && sudo mv mcp-publisher /usr/local/bin/

# Authenticate (opens browser for GitHub OAuth; binds the io.github.Wynelson94/* namespace)
mcp-publisher login github

# Publish (reads server.json at repo root)
cd ~/Projects/longhand
mcp-publisher publish
```

Artifact already in repo: [`/server.json`](../server.json). Validator will check the `<!-- mcp-name: io.github.Wynelson94/longhand -->` marker at the top of the v0.5.5 PyPI README, which is why the version bump is load-bearing.

---

## D3 — `punkpeye/awesome-mcp-servers`

PR adding one line under **🧠 Knowledge & Memory**. Format follows the list's existing entries (alphabetical by GitHub username/project-name within each category).

**Exact line to add** (alphabetized — place where `Wynelson94/...` fits):

```markdown
- [Wynelson94/longhand](https://github.com/Wynelson94/longhand) 🐍 🏠 🍎 🪟 🐧 - Persistent local memory for Claude Code. Indexes every session JSONL verbatim into SQLite + ChromaDB for semantic recall (~126ms) over your entire history. Zero API calls, never summarizes.
```

Badge legend used: 🐍 Python · 🏠 Local service · 🍎 macOS · 🪟 Windows · 🐧 Linux.

**PR steps:**

```bash
gh repo fork punkpeye/awesome-mcp-servers --clone --remote
cd awesome-mcp-servers
git checkout -b add-longhand
# Edit README.md: find "### 🧠 Knowledge & Memory" and insert the line above
#   in alphabetical position.
git add README.md
git commit -m "Add Wynelson94/longhand to Knowledge & Memory"
git push -u origin add-longhand
gh pr create --title "Add Wynelson94/longhand to Knowledge & Memory" --body "Adds [Longhand](https://github.com/Wynelson94/longhand), persistent local memory for Claude Code. Indexes session JSONL files verbatim into SQLite + ChromaDB for ~126ms semantic recall with zero API calls. Published to PyPI (\`pip install longhand\`) and available as a Claude Code plugin. MIT licensed."
```

---

## D4 — `hesreallyhim/awesome-claude-code`

This list does NOT accept PRs. Submission is via the issue template at:
https://github.com/hesreallyhim/awesome-claude-code/issues/new?template=recommend-resource.yml

**Form fields** (paste into the matching inputs):

| Field              | Value                                                                                           |
| ------------------ | ----------------------------------------------------------------------------------------------- |
| Display Name       | Longhand                                                                                        |
| Category           | Tooling                                                                                         |
| Sub-Category       | General                                                                                         |
| Primary Link       | https://github.com/Wynelson94/longhand                                                          |
| Author Name        | Nate Nelson                                                                                     |
| Author Link        | https://github.com/Wynelson94                                                                   |
| License            | MIT                                                                                             |

**Description (1-3 sentences, no emojis, descriptive not promotional):**

> Longhand indexes Claude Code's session JSONL files into a local SQLite + ChromaDB database, giving you semantic search, deterministic file replay, and fuzzy recall across your entire history without any API calls. It installs a SessionEnd hook so new sessions auto-ingest, and exposes 17 MCP tools so Claude itself can query your past work during live sessions. Data never leaves the machine.

**Recommendation checklist** — check all five required boxes:
- [x] I have verified this resource is unique on the list
- [x] The resource is at least one week old (PyPI release: 2026-04-14, well over a week by submission time)
- [x] All links work
- [x] No other open issues for this resource
- [x] I am a human submitter

---

## After all three submissions

Track status:

```bash
gh pr list --author @me --state all --json url,title,state,repository
gh issue list --author @me --state all --repo hesreallyhim/awesome-claude-code
# MCP registry — check live listing:
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=longhand" | jq
```

One week after all three land, pull traffic stats to measure the bump:

```bash
gh api repos/Wynelson94/longhand/traffic/views
gh api repos/Wynelson94/longhand/traffic/clones
```
