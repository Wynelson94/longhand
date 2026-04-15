#!/usr/bin/env bash
# SessionStart check: ensure the `longhand` CLI is on PATH. The plugin's
# MCP server is declared as `longhand mcp-server`; if the binary is
# missing, MCP startup fails and the user sees nothing. Surface it loudly.
#
# Stays silent on the happy path — this runs on every session start.

set -euo pipefail

if command -v longhand >/dev/null 2>&1; then
    exit 0
fi

cat >&2 <<'EOF'
[longhand plugin] The `longhand` CLI is not on PATH.

Install:
    pip install longhand

One-time setup (backfills history, installs SessionEnd hook, registers MCP):
    longhand setup

Docs: https://github.com/Wynelson94/longhand#install
EOF

exit 0
