"""Assert that the canonical package version in pyproject.toml matches
every place it's hardcoded elsewhere in the repo.

Run as part of CI so a stale manifest can never ship unnoticed.

Exit code 0 if everything matches; 1 (with a list of mismatches) otherwise.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent


def canonical_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def check_server_json(expected: str) -> list[str]:
    path = REPO_ROOT / "server.json"
    data = json.loads(path.read_text())
    problems = []
    if data.get("version") != expected:
        problems.append(f"{path.name}: top-level version {data.get('version')!r} != {expected!r}")
    for pkg in data.get("packages", []):
        if pkg.get("version") != expected:
            problems.append(
                f"{path.name}: packages[].version {pkg.get('version')!r} != {expected!r}"
            )
    return problems


def check_plugin_json(expected: str) -> list[str]:
    path = REPO_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(path.read_text())
    if data.get("version") != expected:
        return [f"{path}: version {data.get('version')!r} != {expected!r}"]
    return []


def check_dockerfile(expected: str) -> list[str]:
    path = REPO_ROOT / "Dockerfile"
    text = path.read_text()
    m = re.search(r"longhand==([0-9]+\.[0-9]+\.[0-9]+)", text)
    if not m:
        return [f"{path.name}: no pinned `longhand==X.Y.Z` line found"]
    if m.group(1) != expected:
        return [f"{path.name}: pinned longhand=={m.group(1)} != {expected}"]
    return []


def check_readme_status(expected: str) -> list[str]:
    path = REPO_ROOT / "README.md"
    text = path.read_text()
    m = re.search(r"Status:\s*v([0-9]+\.[0-9]+\.[0-9]+)", text)
    if not m:
        return [f"{path.name}: no `Status: vX.Y.Z` line found"]
    if m.group(1) != expected:
        return [f"{path.name}: Status line v{m.group(1)} != v{expected}"]
    return []


def main() -> int:
    expected = canonical_version()
    problems: list[str] = []
    for check in (check_server_json, check_plugin_json, check_dockerfile, check_readme_status):
        problems.extend(check(expected))

    if problems:
        print(f"Version sync FAILED. pyproject.toml has version {expected!r}.")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(f"Version sync OK. All manifests match {expected}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
