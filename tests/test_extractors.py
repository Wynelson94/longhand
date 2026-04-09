"""Tests for per-event extractors."""

from __future__ import annotations

from longhand.extractors.errors import detect_error
from longhand.extractors.file_refs import extract_file_references
from longhand.extractors.topics import extract_keywords, extract_extensions


# ─── Error detection ───────────────────────────────────────────────────────

def test_detect_python_traceback():
    content = """Traceback (most recent call last):
  File "/tmp/app.py", line 42, in main
    result = 1 / 0
ZeroDivisionError: division by zero"""
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "python"
    assert sig.severity == "error"


def test_detect_node_type_error():
    content = "TypeError: Cannot read properties of undefined (reading 'foo')"
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "node"


def test_detect_cannot_find_module():
    content = "Error: Cannot find module 'express'\n    at Function.Module._resolveFilename"
    sig = detect_error(content)
    assert sig is not None
    # Could match node_module_missing OR bash_error - both are valid


def test_detect_typescript_compile_error():
    content = "src/app.ts(42,10): error TS2304: Cannot find name 'foo'."
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "compile"


def test_detect_pytest_failure():
    content = """FAILED tests/test_foo.py::test_bar - AssertionError: expected 3 but got 2
1 failed, 4 passed in 0.23s"""
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "test"


def test_detect_bash_command_not_found():
    content = "bash: foo: command not found"
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "bash"


def test_detect_rust_compile_error():
    content = """error[E0308]: mismatched types
 --> src/main.rs:5:14
  |
5 |     let x: i32 = "hello";
  |              ^^^ expected `i32`, found `&str`"""
    sig = detect_error(content)
    assert sig is not None
    assert sig.category == "compile"


def test_clean_output_returns_none():
    content = "All tests passed (15 tests in 0.8s)\nCompiled successfully."
    assert detect_error(content) is None


def test_empty_content_returns_none():
    assert detect_error("") is None
    assert detect_error(None) is None


# ─── File reference extraction ─────────────────────────────────────────────

def test_extract_absolute_path():
    text = "Error at /Users/nate/Projects/game/src/main.ts:42:10"
    refs = extract_file_references(text)
    assert any("main.ts" in r for r in refs)


def test_extract_relative_path():
    text = "See src/components/Button.tsx for the implementation"
    refs = extract_file_references(text)
    assert any("Button.tsx" in r for r in refs)


def test_extract_multiple_paths():
    text = """
    Modified /tmp/a.py and /tmp/b.py
    Also touched src/lib/c.ts
    """
    refs = extract_file_references(text)
    assert len(refs) >= 3


def test_ignores_non_code_paths():
    # .tar.gz and .zip shouldn't match as code files
    text = "Downloaded /tmp/data.tar.gz"
    refs = extract_file_references(text)
    # .gz isn't in our code extensions
    assert not any(r.endswith(".tar.gz") for r in refs)


# ─── Topic extraction ──────────────────────────────────────────────────────

def test_extract_keywords_filters_stopwords():
    texts = [
        "I am building a game with phaser and typescript",
        "The game needs webgl rendering and a state machine",
        "Phaser has a built-in physics engine for games",
    ]
    keywords = extract_keywords(texts, top_k=10)
    assert "phaser" in keywords
    assert "typescript" in keywords or "state" in keywords or "webgl" in keywords
    assert "the" not in keywords
    assert "is" not in keywords


def test_extract_keywords_from_empty():
    assert extract_keywords([]) == []
    assert extract_keywords(["", None]) == []  # type: ignore


def test_extract_extensions():
    paths = [
        "/tmp/a.py",
        "/tmp/b.ts",
        "src/c.tsx",
        "/etc/passwd",
        "Cargo.toml",
    ]
    exts = extract_extensions(paths)
    assert "py" in exts
    assert "ts" in exts
    assert "tsx" in exts
    assert "toml" in exts
