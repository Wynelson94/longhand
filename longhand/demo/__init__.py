"""Longhand demo module.

Provides a self-contained walkthrough that lets new users try Longhand
on a tiny pre-built sample corpus without touching their real
~/.claude session history.

The demo:
  1. Creates a temporary store at /tmp/longhand-demo-<timestamp>/
  2. Generates 3 fake Claude Code sessions covering a small Stripe +
     auth-migration workflow on a fictional `demo-shop` project
  3. Ingests them into the temp store
  4. Runs `recall`, `search_in_context`, and `recall_project_status`
     so the user can see what the output looks like before pointing
     Longhand at their own data
  5. Cleans up by default; pass `keep=True` to leave the corpus around
     for further exploration

Entry point: `longhand.demo.run_demo()` (called from the CLI
`longhand demo` subcommand).
"""

from longhand.demo.runner import run_demo

__all__ = ["run_demo"]
