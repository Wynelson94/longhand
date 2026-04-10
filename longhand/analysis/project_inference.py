"""
Project inference from a session.

Canonicalizes the session's cwd and generates a project fingerprint:
display name, aliases, keywords, languages, category.

Deterministic. No LLM.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path
from typing import Any

from longhand.extractors.topics import extract_extensions, extract_keywords
from longhand.types import Event, Session


# Map file extensions → language names
_EXT_TO_LANGUAGE = {
    "py": "python", "pyi": "python",
    "ts": "typescript", "tsx": "typescript",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "kt": "kotlin", "kts": "kotlin",
    "rb": "ruby",
    "php": "php",
    "cs": "csharp",
    "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp",
    "c": "c", "h": "c",
    "swift": "swift",
    "scala": "scala",
    "sql": "sql",
    "html": "html",
    "css": "css", "scss": "css", "sass": "css",
    "sh": "shell", "bash": "shell", "zsh": "shell",
}


# Category inference from touched files / dependencies / project names
_CATEGORY_SIGNALS: list[tuple[str, list[str]]] = [
    ("game", [
        "phaser", "three.js", "threejs", "babylon", "babylonjs", "godot",
        "unity", "pixi", "kaboom", "love2d", "pygame", "macroquad",
        "game.ts", "game.js", "game.py", "gameloop", "sprite",
    ]),
    ("nextjs web app", [
        "next.config", "next.js", "nextjs", "app/layout.tsx", "pages/_app",
    ]),
    ("react web app", [
        "react-dom", "create-react-app", "vite.config", "react.config",
    ]),
    ("python web", [
        "flask", "fastapi", "django", "wsgi.py", "asgi.py", "manage.py",
    ]),
    ("rust cli", ["cargo.toml", "main.rs"]),
    ("go service", ["go.mod", "main.go"]),
    ("cli tool", ["pyproject.toml", "setup.py", "package.json"]),
    ("crm", ["crm", "contacts", "prospects", "deals"]),
    ("mobile app", ["capacitor", "react-native", "expo", "swift", "kotlin"]),
    ("data pipeline", ["airflow", "dbt", "prefect", "dagster"]),
]


# Files/directories that indicate a project root
_PROJECT_ROOT_MARKERS = (
    ".git",           # git repo
    "package.json",   # node
    "pyproject.toml", # python (modern)
    "setup.py",       # python (legacy)
    "Cargo.toml",     # rust
    "go.mod",         # go
    "pom.xml",        # java maven
    "build.gradle",   # java gradle
    "Gemfile",        # ruby
    "composer.json",  # php
    "mix.exs",        # elixir
    "pubspec.yaml",   # dart/flutter
)


def _find_project_root(path: Path, max_walk: int = 8) -> Path:
    """Walk up from `path` to find the nearest directory containing a project marker.

    This collapses subdirectories of the same repo into one canonical project
    path. If no marker is found within `max_walk` levels, returns `path` as-is.
    """
    current = path
    for _ in range(max_walk):
        try:
            for marker in _PROJECT_ROOT_MARKERS:
                if (current / marker).exists():
                    return current
            # Also match *.xcodeproj wildcard
            try:
                if any(current.glob("*.xcodeproj")):
                    return current
            except (OSError, PermissionError):
                pass
        except (OSError, PermissionError):
            break

        parent = current.parent
        if parent == current:
            break
        current = parent

    return path


def _canonicalize_path(path: str | None) -> str | None:
    """Resolve and walk up to the project root (git/package marker)."""
    if not path:
        return None
    try:
        resolved = Path(path).resolve()
        if resolved.is_file():
            resolved = resolved.parent
        root = _find_project_root(resolved)
        return str(root)
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
    haystack_parts = [display_name.lower(), " ".join(keywords).lower()] + [p.lower() for p in file_paths]
    haystack = " ".join(haystack_parts)

    for category, signals in _CATEGORY_SIGNALS:
        for signal in signals:
            if signal.lower() in haystack:
                return category

    return None


def infer_project(session: Session, events: list[Event]) -> dict[str, Any]:
    """Build a ProjectFingerprint dict from a session and its events."""
    canonical = _canonicalize_path(session.cwd or session.project_path)
    if not canonical:
        # No cwd — use a synthetic project keyed on the transcript file's parent
        canonical = str(Path(session.transcript_path).parent.resolve())

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
    languages = sorted({_EXT_TO_LANGUAGE.get(e, "") for e in extensions if e in _EXT_TO_LANGUAGE} - {""})

    # Extract keywords from user messages + thinking + file basenames
    file_basenames = [Path(p).name for p in touched_files]
    keywords = extract_keywords(user_texts + thinking_texts + file_basenames, top_k=15, min_count=1)

    # Category inference
    category = _infer_category(touched_files, keywords, display_name)

    # Aliases
    aliases = _generate_aliases(display_name, canonical, category)

    # Count file edits for new_edits increment
    new_edits = sum(
        1
        for e in events
        if (e.event_type if isinstance(e.event_type, str) else e.event_type.value) == "tool_call"
        and e.file_operation in ("edit", "write", "multi_edit", "notebook_edit")
    )

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
        "new_edits": new_edits,
    }
