"""
Project inference from a session.

Canonicalizes the session's cwd and generates a project fingerprint:
display name, aliases, keywords, languages, category.

Deterministic. No LLM.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

from longhand.extractors.topics import extract_extensions, extract_keywords
from longhand.types import Event, Session

# Reserved bucket for sessions that can't honestly be attributed to a project:
# no cwd at all, or a markerless cwd in a location that must never mint a
# project (home root, temp dirs, plugin caches, tool-results, pytest dirs).
# Fixed id — NOT derived from _project_id_for — so every such session lands in
# one stable bucket instead of minting junk projects per path.
UNATTRIBUTED_PROJECT_ID = "p_unattributed"
UNATTRIBUTED_CANONICAL_PATH = "unattributed://"

# Map file extensions → language names
_EXT_TO_LANGUAGE = {
    "py": "python",
    "pyi": "python",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "rb": "ruby",
    "php": "php",
    "cs": "csharp",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "c": "c",
    "h": "c",
    "swift": "swift",
    "scala": "scala",
    "sql": "sql",
    "html": "html",
    "css": "css",
    "scss": "css",
    "sass": "css",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
}


# Category inference from touched files / dependencies / project names
_CATEGORY_SIGNALS: list[tuple[str, list[str]]] = [
    (
        "game",
        [
            "phaser",
            "three.js",
            "threejs",
            "babylon",
            "babylonjs",
            "godot",
            "unity",
            "pixi",
            "kaboom",
            "love2d",
            "pygame",
            "macroquad",
            "game.ts",
            "game.js",
            "game.py",
            "gameloop",
            "sprite",
        ],
    ),
    (
        "nextjs web app",
        [
            "next.config",
            "next.js",
            "nextjs",
            "app/layout.tsx",
            "pages/_app",
        ],
    ),
    (
        "react web app",
        [
            "react-dom",
            "create-react-app",
            "vite.config",
            "react.config",
        ],
    ),
    (
        "python web",
        [
            "flask",
            "fastapi",
            "django",
            "wsgi.py",
            "asgi.py",
            "manage.py",
        ],
    ),
    ("rust cli", ["cargo.toml", "main.rs"]),
    ("go service", ["go.mod", "main.go"]),
    ("cli tool", ["pyproject.toml", "setup.py", "package.json"]),
    ("crm", ["crm", "contacts", "prospects", "deals"]),
    ("mobile app", ["capacitor", "react-native", "expo", "swift", "kotlin"]),
    ("data pipeline", ["airflow", "dbt", "prefect", "dagster"]),
]


# Files/directories that indicate a project root. `.git` is handled
# separately in the walk (highest .git wins over any nearer non-git marker —
# monorepo packages must not split into per-package projects).
_NON_GIT_ROOT_MARKERS = (
    "package.json",  # node
    "pyproject.toml",  # python (modern)
    "setup.py",  # python (legacy)
    "Cargo.toml",  # rust
    "go.mod",  # go
    "pom.xml",  # java maven
    "build.gradle",  # java gradle
    "Gemfile",  # ruby
    "composer.json",  # php
    "mix.exs",  # elixir
    "pubspec.yaml",  # dart/flutter
)
_PROJECT_ROOT_MARKERS = (".git", *_NON_GIT_ROOT_MARKERS)


def _find_project_root(path: Path, max_walk: int = 8) -> Path:
    """Walk up from `path` to find the nearest directory containing a project marker.

    This collapses subdirectories of the same repo into one canonical project
    path. If no marker is found within `max_walk` levels, returns `path` as-is.
    """
    root = find_project_root_strict(path, max_walk=max_walk)
    return root if root is not None else path


def find_project_root_strict(path: Path, max_walk: int = 8) -> Path | None:
    """Walk up from `path` looking for a project marker; return None if none found.

    `.git` outranks every other marker, and the HIGHEST `.git` within the walk
    wins: in a monorepo (`repo/.git` + `repo/apps/web/package.json`), sessions
    in `apps/web` must attribute to `repo`, not split into a per-package
    project. Nested repos likewise collapse into the outermost one. Only when
    no `.git` is found does the nearest non-git marker decide.

    Unlike `_find_project_root`, this returns None instead of falling back to
    the input path. Callers that need to distinguish "real project" from
    "arbitrary directory" should use this.
    """
    current = path
    git_root: Path | None = None
    marker_root: Path | None = None
    for _ in range(max_walk):
        try:
            if (current / ".git").exists():
                git_root = current  # keep walking — a higher .git wins
            elif marker_root is None:
                for marker in _NON_GIT_ROOT_MARKERS:
                    if (current / marker).exists():
                        marker_root = current
                        break
                else:
                    # Also match *.xcodeproj wildcard
                    try:
                        if any(current.glob("*.xcodeproj")):
                            marker_root = current
                    except (OSError, PermissionError):
                        pass
        except (OSError, PermissionError):
            break

        parent = current.parent
        if parent == current:
            break
        current = parent

    return git_root or marker_root


def _true_case(path: Path) -> Path:
    """Recover the on-disk casing of an existing path, component by component.

    On case-insensitive filesystems (macOS APFS), `~/projects/Foo` and
    `~/Projects/foo` are the same directory but hash to different project ids.
    Normalizing to the filesystem's own casing makes every spelling of a path
    mint the same project. Non-existent components (and case-sensitive
    filesystems, where only the exact casing exists) pass through unchanged.
    """
    try:
        out = Path(path.anchor)
        for comp in path.parts[len(out.parts) :]:
            candidate = out / comp
            if candidate.exists():
                # Fast path — but on a case-insensitive FS this matches any
                # casing, so recover the real name from the parent listing.
                match = next(
                    (c for c in out.iterdir() if c.name.lower() == comp.lower()),
                    None,
                )
                out = match if match is not None else candidate
            else:
                out = candidate
        return out
    except (OSError, PermissionError):
        return path


def _is_reserved_path(path: Path) -> bool:
    """True for locations that must never mint a project of their own.

    Only consulted when NO project marker was found — a real repo checked out
    under /tmp still attributes normally. These are the junk-project sources
    observed on real corpora: the $HOME catch-all, temp/scratch dirs, pytest
    tmpdirs, and Claude Code's own internal directories (plugin caches,
    tool-results, the transcript store itself).
    """
    home = Path.home()
    if path == home:
        return True
    claude_dir = home / ".claude"
    if path == claude_dir or claude_dir in path.parents:
        return True
    parts = path.parts
    if any(p.startswith("pytest-") for p in parts):
        return True
    if "plugin-cache" in parts or "tool-results" in parts:
        return True
    tmp_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp"),
        Path("/private/tmp"),
        Path("/var/folders"),
        Path("/private/var/folders"),
    }
    return any(path == root or root in path.parents for root in tmp_roots)


def _unattributed_fingerprint(session: Session) -> dict[str, Any]:
    """The reserved bucket fingerprint. Keywords/languages deliberately empty —
    hundreds of unrelated sessions share this row, and merging their keywords
    would turn it into an alias magnet that hijacks fuzzy project matching."""
    return {
        "project_id": UNATTRIBUTED_PROJECT_ID,
        "canonical_path": UNATTRIBUTED_CANONICAL_PATH,
        "display_name": "unattributed",
        "aliases": ["unattributed"],
        "keywords": [],
        "languages": [],
        "category": None,
        "first_seen": session.started_at.isoformat(),
        "last_seen": session.ended_at.isoformat(),
    }


def _canonicalize_path(path: str | None) -> str | None:
    """Resolve and walk up to the project root (git/package marker)."""
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        if resolved.is_file():
            resolved = resolved.parent
        root = _find_project_root(resolved)
        return str(_true_case(root))
    except Exception:
        return path


def _project_id_for(canonical_path: str) -> str:
    return "p_" + hashlib.sha1(canonical_path.encode("utf-8")).hexdigest()[:16]


def _display_name(canonical_path: str) -> str:
    name = Path(canonical_path).name
    # Humanize: replace dashes/underscores with spaces, collapse whitespace
    humanized = re.sub(r"[-_]", " ", name).strip()
    return humanized or name


def _generate_aliases(display_name: str, canonical_path: str, category: str | None) -> list[str]:
    aliases: set[str] = set()
    aliases.add(display_name.lower())

    # Raw directory name
    raw_name = Path(canonical_path).name.lower()
    aliases.add(raw_name)

    # Split on separators for partial matches
    for part in re.split(r"[-_\s]+", display_name.lower()):
        if len(part) >= 3:
            aliases.add(part)

    # Category as an alias
    if category:
        aliases.add(category.lower())
        # "the game", "my game" style fuzzy hooks
        if " " not in category:
            aliases.add(f"the {category}")

    return sorted(a for a in aliases if a)


def _infer_category(
    file_paths: list[str],
    keywords: list[str],
    display_name: str,
) -> str | None:
    haystack_parts = [display_name.lower(), " ".join(keywords).lower()] + [
        p.lower() for p in file_paths
    ]
    haystack = " ".join(haystack_parts)

    for category, signals in _CATEGORY_SIGNALS:
        for signal in signals:
            if signal.lower() in haystack:
                return category

    return None


def infer_project(session: Session, events: list[Event]) -> dict[str, Any]:
    """Build a ProjectFingerprint dict from a session and its events.

    Sessions with no cwd, or a markerless cwd in a reserved location (home
    root, temp/pytest dirs, Claude Code internals), land in the reserved
    `unattributed` bucket instead of minting a junk project. Everything else
    keys off the (case-normalized) project root as before.
    """
    raw = session.cwd or session.project_path
    if not raw:
        # Pre-v0.12 this minted a synthetic project from the transcript file's
        # parent — i.e. Claude Code's own storage dir — which is exactly the
        # "launch slug" junk-project source.
        return _unattributed_fingerprint(session)

    try:
        resolved = Path(raw).resolve()
        if resolved.is_file():
            resolved = resolved.parent
        root = find_project_root_strict(resolved)
        if root is None and _is_reserved_path(resolved):
            return _unattributed_fingerprint(session)
        canonical = str(_true_case(root if root is not None else resolved))
    except Exception:
        canonical = raw

    project_id = _project_id_for(canonical)
    display_name = _display_name(canonical)

    # Collect touched files
    touched_files: list[str] = []
    user_texts: list[str] = []
    thinking_texts: list[str] = []

    for e in events:
        etype = e.event_type if isinstance(e.event_type, str) else e.event_type.value
        if e.file_path:
            touched_files.append(e.file_path)
        if etype == "user_message" and e.content:
            user_texts.append(e.content)
        if etype == "assistant_thinking" and e.content:
            thinking_texts.append(e.content)

    # Extract extensions → languages
    extensions = extract_extensions(touched_files)
    languages = sorted(
        {_EXT_TO_LANGUAGE.get(e, "") for e in extensions if e in _EXT_TO_LANGUAGE} - {""}
    )

    # Extract keywords from user messages + thinking + file basenames
    file_basenames = [Path(p).name for p in touched_files]
    keywords = extract_keywords(user_texts + thinking_texts + file_basenames, top_k=15, min_count=1)

    # Category inference
    category = _infer_category(touched_files, keywords, display_name)

    # Aliases
    aliases = _generate_aliases(display_name, canonical, category)

    started_iso = session.started_at.isoformat()
    ended_iso = session.ended_at.isoformat()

    return {
        "project_id": project_id,
        "canonical_path": canonical,
        "display_name": display_name,
        "aliases": aliases,
        "keywords": keywords,
        "languages": languages,
        "category": category,
        "first_seen": started_iso,
        "last_seen": ended_iso,
    }
