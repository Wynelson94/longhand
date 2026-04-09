"""
Topic extraction for sessions.

Deterministic keyword extraction from user messages, thinking blocks,
and touched file paths. No LLM. Drives project alias generation and
session summary embeddings.
"""

from __future__ import annotations

import re
from collections import Counter


# Stopwords we never want as topics
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "as", "into", "through", "during",
    "before", "after", "above", "below", "to", "from", "up", "down", "in",
    "out", "on", "off", "over", "under", "again", "further", "once",
    "here", "there", "all", "any", "both", "each", "few", "more", "most",
    "other", "some", "such", "no", "nor", "not", "only", "own", "same",
    "so", "than", "too", "very", "can", "will", "just", "should", "now",
    "this", "that", "these", "those", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "doing",
    "would", "could", "might", "must", "shall", "may",
    "i", "me", "my", "you", "your", "we", "our", "us", "it", "its",
    "claude", "user", "assistant", "please", "thanks", "thank", "yes", "no",
    "okay", "ok", "sure", "alright", "right", "wrong", "good", "bad",
    "like", "want", "need", "think", "know", "see", "get", "got", "make",
    "made", "go", "going", "went", "come", "came", "take", "took", "give",
    "gave", "say", "said", "tell", "told", "ask", "asked", "try", "tried",
    "work", "works", "working", "worked", "use", "using", "used",
    "one", "two", "three", "first", "second", "third", "new", "old",
    "let", "lets", "much", "many", "some", "any", "also", "well",
    "still", "even", "really", "actually", "probably", "definitely",
    "something", "nothing", "everything", "anything",
    "file", "files", "line", "lines", "code", "error", "errors",
    "function", "functions", "method", "methods", "class", "classes",
    "add", "added", "adding", "remove", "removed", "removing",
    "fix", "fixed", "fixing", "update", "updated", "updating",
    "build", "built", "building", "run", "running", "ran",
    "test", "tests", "testing", "tested",
    "app", "apps", "project", "projects",
}


def _tokenize(text: str) -> list[str]:
    """Lowercase and split on non-word boundaries. Keeps hyphens and underscores."""
    return re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower())


def extract_keywords(texts: list[str], top_k: int = 20, min_count: int = 2) -> list[str]:
    """Extract the top-K most frequent meaningful tokens across a set of texts.

    - Lowercased
    - Stopwords removed
    - Short tokens removed (< 3 chars)
    - Must appear at least `min_count` times to be considered (unless top_k is tight)
    """
    counter: Counter[str] = Counter()
    for text in texts:
        if not text:
            continue
        tokens = _tokenize(text)
        for token in tokens:
            if token in _STOPWORDS:
                continue
            if len(token) < 3:
                continue
            if token.isdigit():
                continue
            counter[token] += 1

    # Require min_count unless we don't have enough results
    frequent = [t for t, c in counter.most_common() if c >= min_count]
    if len(frequent) < top_k:
        # Fall back to include singletons
        frequent = [t for t, _ in counter.most_common(top_k)]

    return frequent[:top_k]


def extract_extensions(file_paths: list[str]) -> list[str]:
    """Return unique file extensions (lowercased, without dot) from a list of paths."""
    exts: set[str] = set()
    for path in file_paths:
        if "." in path:
            ext = path.rsplit(".", 1)[-1].lower()
            if 0 < len(ext) <= 6 and ext.isalnum():
                exts.add(ext)
    return sorted(exts)
