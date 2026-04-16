"""Longhand CLI package.

The entry point `longhand` (declared in pyproject.toml as `longhand.cli:app`)
resolves to this package — we re-export `app` from the commands submodule so
the entry point continues to work after the cli.py → cli/ package conversion.
"""

from longhand.cli._commands import app

__all__ = ["app"]
