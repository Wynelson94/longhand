"""Tests for the JSONL parser."""

from __future__ import annotations

import json

from longhand.parser import JSONLParser, discover_sessions
from longhand.types import EventType, FileOperation


def test_discover_sessions_filters_subagent_jsonls():
    """Regression: subagent JSONLs under .../<session>/subagents/<sub>.jsonl
    are referenced from the parent session's events, not standalone sessions.
    Treating them as top-level was the v0.6.0 bug that caused reconcile to
    re-ingest them and double-count session totals.

    Uses tempfile.TemporaryDirectory directly because pytest's tmp_path lives
    under `pytest-of-<user>/...` which discover_sessions filters out (a
    separate guardrail against ingesting test artifacts from the live home).
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="longhand_canary_") as raw:
        projects_dir = Path(raw) / "projects"
        project = projects_dir / "myproj"
        project.mkdir(parents=True)

        # Top-level session — should be discovered.
        top_session = project / "session-abc.jsonl"
        top_session.write_text("")

        # Subagent transcript — should be filtered out.
        subagent_dir = project / "session-abc" / "subagents"
        subagent_dir.mkdir(parents=True)
        sub_transcript = subagent_dir / "agent-xyz.jsonl"
        sub_transcript.write_text("")

        found = discover_sessions(projects_dir)
        found_names = {p.name for p in found}

        assert "session-abc.jsonl" in found_names
        assert "agent-xyz.jsonl" not in found_names, (
            f"subagent transcript leaked into discover_sessions output: {found}"
        )


def test_parse_sample_session(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    # Should have events for each block, not each entry
    assert len(events) > 0

    # Event types
    types = [e.event_type for e in events]
    assert EventType.FILE_SNAPSHOT.value in types
    assert EventType.USER_MESSAGE.value in types
    assert EventType.ASSISTANT_THINKING.value in types
    assert EventType.ASSISTANT_TEXT.value in types
    assert EventType.TOOL_CALL.value in types
    assert EventType.TOOL_RESULT.value in types


def test_multi_block_assistant_splits_into_events(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    # The assistant message with thinking+text+tool_use should produce 3 events
    thinking = [e for e in events if e.event_type == EventType.ASSISTANT_THINKING.value]
    texts = [e for e in events if e.event_type == EventType.ASSISTANT_TEXT.value]
    tools = [e for e in events if e.event_type == EventType.TOOL_CALL.value]

    assert len(thinking) >= 1
    assert len(texts) >= 1
    assert len(tools) >= 2  # Edit + Write
    assert thinking[0].content == "User wants a readme edit."


def test_edit_tool_call_captures_diff(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    edit_call = next(
        (e for e in events if e.event_type == EventType.TOOL_CALL.value and e.tool_name == "Edit"),
        None,
    )
    assert edit_call is not None
    assert edit_call.file_path == "/tmp/test-project/README.md"
    assert edit_call.file_operation == FileOperation.EDIT.value
    assert edit_call.old_content == "Old content"
    assert edit_call.new_content == "New content"


def test_write_tool_call_captures_full_content(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    write_call = next(
        (e for e in events if e.event_type == EventType.TOOL_CALL.value and e.tool_name == "Write"),
        None,
    )
    assert write_call is not None
    assert write_call.file_path == "/tmp/test-project/new.txt"
    assert write_call.file_operation == FileOperation.WRITE.value
    assert write_call.new_content == "Hello, World!"


def test_tool_result_linked_by_tool_use_id(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())

    results = [e for e in events if e.event_type == EventType.TOOL_RESULT.value]
    assert len(results) == 1
    assert results[0].tool_use_id == "tool-1"
    assert "updated successfully" in results[0].content


def test_session_summary(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    assert session.session_id == "test-session-1"
    assert session.project_path == "/tmp/test-project"
    assert session.git_branch == "main"
    assert session.user_message_count >= 1
    assert session.tool_call_count >= 2
    assert session.file_edit_count >= 2
    assert session.model == "claude-sonnet-4-6"


def test_timestamps_are_timezone_aware(sample_session_file):
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    for e in events:
        assert e.timestamp.tzinfo is not None


def test_raw_is_preserved(sample_session_file):
    """Every event should preserve the full original JSON entry in raw."""
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    for e in events:
        assert e.raw is not None
        assert isinstance(e.raw, dict)
        # Non-snapshot events should have a type field in the raw entry
        if e.event_type != EventType.FILE_SNAPSHOT.value:
            assert "type" in e.raw


def _write_multi_cwd_jsonl(
    path, cwds: list[str], session_id: str = "multi-cwd-session"
) -> None:
    """Write a minimal JSONL where each entry uses a cwd from `cwds` in order."""
    entries = []
    for i, cwd in enumerate(cwds):
        entries.append(
            {
                "type": "user",
                "uuid": f"evt-{i}",
                "sessionId": session_id,
                "timestamp": f"2026-04-22T12:{i:02d}:00Z",
                "cwd": cwd,
                "message": {"role": "user", "content": f"hi from {cwd}"},
            }
        )
    path.write_text("\n".join(json.dumps(e) for e in entries))


def test_build_session_picks_mode_non_home_cwd(tmp_path, monkeypatch):
    """A session that cd's between $HOME and a project dir should attribute to the project.

    Previously `build_session` took the first-event cwd, which was $HOME when
    Claude Code launched from the user's home dir. That caused project inference
    to set project_id=NULL on real work sessions. Now we tally all event cwds,
    filter out $HOME and non-project dirs, and pick the most-common remainder.
    """
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "home" / "Projects" / "real-project"
    project.mkdir(parents=True)
    (project / ".git").mkdir()  # marker so find_project_root_strict resolves it

    monkeypatch.setenv("HOME", str(home))

    jsonl_path = tmp_path / "session.jsonl"
    # First event: home. Then 4 events in the project. One event elsewhere.
    _write_multi_cwd_jsonl(
        jsonl_path,
        [
            str(home),
            str(project),
            str(project),
            str(project),
            str(project),
        ],
    )

    parser = JSONLParser(jsonl_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    # cwd should be the project, not $HOME — despite first event being $HOME.
    assert session.cwd == str(project)
    assert session.project_path == str(project)


def test_build_session_falls_back_to_first_cwd_when_no_project_marker(tmp_path, monkeypatch):
    """If no cwd resolves to a project root, fall back to the existing behaviour."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # Both cwds are bare directories (no .git, no pyproject.toml, etc.)
    bare_a = tmp_path / "a"
    bare_a.mkdir()
    bare_b = tmp_path / "b"
    bare_b.mkdir()

    jsonl_path = tmp_path / "session.jsonl"
    _write_multi_cwd_jsonl(jsonl_path, [str(bare_a), str(bare_b)])

    parser = JSONLParser(jsonl_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    # No project markers → fall back to first-event cwd.
    assert session.cwd == str(bare_a)


def test_build_session_picks_most_common_of_multiple_projects(tmp_path, monkeypatch):
    """Multi-project sessions should attribute to the project with the most events."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    project_a = tmp_path / "home" / "Projects" / "a"
    project_a.mkdir(parents=True)
    (project_a / ".git").mkdir()
    project_b = tmp_path / "home" / "Projects" / "b"
    project_b.mkdir(parents=True)
    (project_b / ".git").mkdir()

    jsonl_path = tmp_path / "session.jsonl"
    # 5 events in A, 2 events in B — A should win.
    _write_multi_cwd_jsonl(
        jsonl_path,
        [str(project_a)] * 5 + [str(project_b)] * 2,
    )

    parser = JSONLParser(jsonl_path)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    assert session.cwd == str(project_a)


def test_user_image_block_is_replaced_with_placeholder(tmp_path):
    """User messages with pasted images must not leak base64 into event content.

    Prior to v0.5.11 the parser json.dumps'd any non-text user block, which
    embedded the full base64 payload into the event content. That payload then
    flowed into segment summaries and polluted keyword extraction with random
    alphanumeric tokens from the binary data.
    """
    fake_base64 = "A" * 500  # stand-in for a real JPEG payload
    entries = [
        {
            "type": "user",
            "uuid": "user-with-image",
            "sessionId": "img-session",
            "timestamp": "2026-04-17T00:00:00Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "here's a screenshot"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": fake_base64,
                        },
                    },
                ],
            },
        },
    ]
    jsonl_path = tmp_path / "image-session.jsonl"
    jsonl_path.write_text("\n".join(json.dumps(e) for e in entries))

    parser = JSONLParser(jsonl_path)
    events = [e for e in parser.parse_events() if e.event_type == EventType.USER_MESSAGE.value]

    # Expect two events: the text block and the image placeholder.
    contents = [e.content for e in events]
    assert "here's a screenshot" in contents
    assert "[image: image/jpeg]" in contents
    # The base64 payload must not appear anywhere.
    joined = " ".join(contents)
    assert fake_base64 not in joined
    assert "base64" not in joined
