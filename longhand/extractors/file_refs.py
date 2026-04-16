"""
Extract filesystem path references from text.

Used to cross-reference bash output and error messages back to the files
involved. Deterministic regex-based, no LLM.
"""

from __future__ import annotations

import re

# Match plausible file paths in text:
#  - absolute paths starting with /
#  - relative paths with slashes and common extensions
#  - node_modules-style paths
_PATH_PATTERNS = [
    # Absolute paths with an extension (most specific)
    re.compile(r"(?<![\w/])/[\w./-]+\.[a-zA-Z0-9]{1,6}(?:\:\d+(?:\:\d+)?)?"),
    # Relative paths with an extension
    re.compile(r"(?<![\w/])[\w./-]+/[\w./-]+\.[a-zA-Z0-9]{1,6}"),
    # src/foo/bar.ts style (starts with a known code folder)
    re.compile(r"(?<![\w/])(?:src|lib|app|pages|components|tests?|spec|cmd|pkg|internal)/[\w./-]+\.[a-zA-Z0-9]{1,6}"),
]

# Common extensions we care about for code
_CODE_EXTENSIONS = {
    "py", "ts", "tsx", "js", "jsx", "mjs", "cjs",
    "go", "rs", "java", "kt", "scala", "swift",
    "rb", "php", "cs", "cpp", "c", "h", "hpp",
    "sql", "md", "yaml", "yml", "json", "toml",
    "html", "css", "scss", "sass",
    "sh", "bash", "zsh", "fish",
}


def extract_file_references(text: str, max_refs: int = 50) -> list[str]:
    """Return a deduplicated list of file paths found in the text."""
    if not text:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for pattern in _PATH_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(0)
            # Strip trailing line:col suffix for canonical form
            path = re.sub(r":\d+(:\d+)?$", "", raw)
            # Must have a code-like extension
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if ext not in _CODE_EXTENSIONS:
                continue
            if path in seen:
                continue
            seen.add(path)
            found.append(path)
            if len(found) >= max_refs:
                return found

    return found
