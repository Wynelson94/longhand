"""Bump the canonical version in pyproject.toml and every sibling manifest
that check_version_sync.py validates.

Run before committing a release. Mirrors check_version_sync.py exactly — if
the check knows about a file, this script updates it. Future siblings: add
both an entry here and a check there.

Usage:
    python scripts/bump_version.py 0.9.1

Refuses to downgrade. Refuses non-semver input. Idempotent — running with
the version already set is a no-op (and a clean exit).
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
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def parse_semver(s: str) -> tuple[int, int, int]:
    if not SEMVER_RE.match(s):
        raise SystemExit(f"Not a semver string: {s!r}")
    return tuple(int(p) for p in s.split("."))  # type: ignore[return-value]


def current_version() -> str:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return data["project"]["version"]


def bump_pyproject(new: str) -> bool:
    path = REPO_ROOT / "pyproject.toml"
    text = path.read_text()
    new_text = re.sub(
        r'^version\s*=\s*"[^"]+"',
        f'version = "{new}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def bump_server_json(new: str) -> bool:
    path = REPO_ROOT / "server.json"
    data = json.loads(path.read_text())
    changed = False
    if data.get("version") != new:
        data["version"] = new
        changed = True
    for pkg in data.get("packages", []):
        if pkg.get("version") != new:
            pkg["version"] = new
            changed = True
    if changed:
        path.write_text(json.dumps(data, indent=2) + "\n")
    return changed


def bump_plugin_json(new: str) -> bool:
    path = REPO_ROOT / ".claude-plugin" / "plugin.json"
    data = json.loads(path.read_text())
    if data.get("version") == new:
        return False
    data["version"] = new
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def bump_dockerfile(new: str) -> bool:
    path = REPO_ROOT / "Dockerfile"
    text = path.read_text()
    new_text = re.sub(
        r"longhand==[0-9]+\.[0-9]+\.[0-9]+",
        f"longhand=={new}",
        text,
    )
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def bump_readme(new: str) -> bool:
    path = REPO_ROOT / "README.md"
    text = path.read_text()
    new_text = re.sub(
        r"(Status:\s*)v[0-9]+\.[0-9]+\.[0-9]+",
        rf"\1v{new}",
        text,
        count=1,
    )
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python scripts/bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    new = argv[1]
    new_tuple = parse_semver(new)
    cur = current_version()
    cur_tuple = parse_semver(cur)

    if new_tuple < cur_tuple:
        print(
            f"Refusing to downgrade {cur} → {new}. "
            f"Run with the same or higher version.",
            file=sys.stderr,
        )
        return 1

    bumps = {
        "pyproject.toml": bump_pyproject(new),
        "server.json": bump_server_json(new),
        ".claude-plugin/plugin.json": bump_plugin_json(new),
        "Dockerfile": bump_dockerfile(new),
        "README.md": bump_readme(new),
    }

    if not any(bumps.values()):
        print(f"All manifests already at {new}. No changes made.")
        return 0

    print(f"Bumped {cur} → {new}:")
    for name, changed in bumps.items():
        print(f"  {'✓' if changed else '·'} {name}")
    print("\nNext steps:")
    print(f"  1. Update CHANGELOG.md with a [{new}] section")
    print("  2. python scripts/check_version_sync.py  # confirm")
    print("  3. ruff check && pytest")
    print(f"  4. git commit -am 'v{new}: ...' && git push")
    print(f"  5. git tag -a v{new} -m '...' && git push origin v{new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
