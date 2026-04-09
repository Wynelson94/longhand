"""Tests for the replay engine — the killer feature."""

from __future__ import annotations

from longhand.parser import JSONLParser
from longhand.replay import ReplayEngine, _apply_edit, _apply_multi_edit


def test_apply_edit_single_occurrence():
    content = "foo bar foo baz"
    result = _apply_edit(content, "foo", "FOO", replace_all=False)
    assert result == "FOO bar foo baz"


def test_apply_edit_replace_all():
    content = "foo bar foo baz"
    result = _apply_edit(content, "foo", "FOO", replace_all=True)
    assert result == "FOO bar FOO baz"


def test_apply_edit_missing_leaves_content_unchanged():
    content = "hello world"
    result = _apply_edit(content, "nope", "yes")
    assert result == "hello world"


def test_apply_multi_edit_sequential():
    content = "apple banana cherry"
    edits = [
        {"old_string": "apple", "new_string": "APPLE"},
        {"old_string": "cherry", "new_string": "CHERRY"},
    ]
    result = _apply_multi_edit(content, edits)
    assert result == "APPLE banana CHERRY"


def test_replay_reconstructs_file_after_multiple_edits(multi_edit_session_file, temp_store):
    parser = JSONLParser(multi_edit_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    engine = ReplayEngine(temp_store.sqlite)
    state = engine.file_state_at(
        file_path="/tmp/test/sample.py",
        session_id="edit-session",
    )

    assert state is not None
    assert state.edits_applied == 4  # 1 write + 3 edits
    assert "def greet():" in state.content
    assert 'print(\'hello world\')' in state.content
    assert '"""Say hello."""' in state.content
    # The original 'def hello():' should be gone
    assert "def hello():" not in state.content


def test_replay_at_specific_event(multi_edit_session_file, temp_store):
    """Reconstruct the file state partway through the session."""
    parser = JSONLParser(multi_edit_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)
    temp_store.ingest_session(session, events)

    engine = ReplayEngine(temp_store.sqlite)

    # At edit-2 (after first rename + print update, but before docstring)
    state = engine.file_state_at(
        file_path="/tmp/test/sample.py",
        session_id="edit-session",
        at_event_id="edit-2",
    )

    assert state is not None
    assert "def greet():" in state.content
    assert "print('hello world')" in state.content
    # Docstring should not yet exist
    assert '"""' not in state.content
