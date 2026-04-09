"""Shared pytest fixtures for Longhand tests."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest


def _line(entry: dict[str, Any]) -> str:
    return json.dumps(entry) + "\n"


@pytest.fixture
def sample_session_file(tmp_path: Path) -> Path:
    """A minimal realistic Claude Code session file with the main event types."""
    path = tmp_path / "session.jsonl"

    entries = [
        # File history snapshot (boundary event)
        {
            "type": "file-history-snapshot",
            "messageId": "snap-1",
            "sessionId": "test-session-1",
            "snapshot": {"messageId": "snap-1", "timestamp": "2026-04-09T10:00:00.000Z"},
        },
        # User message
        {
            "type": "user",
            "uuid": "user-1",
            "parentUuid": None,
            "sessionId": "test-session-1",
            "timestamp": "2026-04-09T10:00:01.000Z",
            "cwd": "/tmp/test-project",
            "gitBranch": "main",
            "isSidechain": False,
            "message": {"role": "user", "content": "Edit the readme"},
        },
        # Assistant thinking + text + tool_use (multi-block content)
        {
            "type": "assistant",
            "uuid": "asst-1",
            "parentUuid": "user-1",
            "sessionId": "test-session-1",
            "timestamp": "2026-04-09T10:00:02.000Z",
            "cwd": "/tmp/test-project",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "User wants a readme edit."},
                    {"type": "text", "text": "Let me edit the README."},
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/test-project/README.md",
                            "old_string": "Old content",
                            "new_string": "New content",
                            "replace_all": False,
                        },
                    },
                ],
            },
        },
        # Tool result
        {
            "type": "user",
            "uuid": "result-1",
            "parentUuid": "asst-1",
            "sessionId": "test-session-1",
            "timestamp": "2026-04-09T10:00:03.000Z",
            "cwd": "/tmp/test-project",
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "The file has been updated successfully.",
                    }
                ],
            },
            "toolUseResult": {"success": True},
        },
        # Write tool (full content)
        {
            "type": "assistant",
            "uuid": "asst-2",
            "parentUuid": "result-1",
            "sessionId": "test-session-1",
            "timestamp": "2026-04-09T10:00:04.000Z",
            "cwd": "/tmp/test-project",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-2",
                        "name": "Write",
                        "input": {
                            "file_path": "/tmp/test-project/new.txt",
                            "content": "Hello, World!",
                        },
                    }
                ],
            },
        },
    ]

    with path.open("w") as f:
        for entry in entries:
            f.write(_line(entry))

    return path


@pytest.fixture
def multi_edit_session_file(tmp_path: Path) -> Path:
    """A session with multiple sequential edits to the same file."""
    path = tmp_path / "multi-edit.jsonl"

    def edit_entry(uuid: str, parent: str | None, ts: str, old: str, new: str) -> dict[str, Any]:
        return {
            "type": "assistant",
            "uuid": uuid,
            "parentUuid": parent,
            "sessionId": "edit-session",
            "timestamp": ts,
            "cwd": "/tmp/test",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"tool-{uuid}",
                        "name": "Edit",
                        "input": {
                            "file_path": "/tmp/test/sample.py",
                            "old_string": old,
                            "new_string": new,
                            "replace_all": False,
                        },
                    }
                ],
            },
        }

    entries = [
        # Write creates the file
        {
            "type": "assistant",
            "uuid": "write-1",
            "parentUuid": None,
            "sessionId": "edit-session",
            "timestamp": "2026-04-09T12:00:00.000Z",
            "cwd": "/tmp/test",
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-write-1",
                        "name": "Write",
                        "input": {
                            "file_path": "/tmp/test/sample.py",
                            "content": "def hello():\n    print('hi')\n",
                        },
                    }
                ],
            },
        },
        # Edit 1: rename function
        edit_entry(
            "edit-1", "write-1", "2026-04-09T12:00:01.000Z",
            old="def hello():",
            new="def greet():",
        ),
        # Edit 2: update print statement
        edit_entry(
            "edit-2", "edit-1", "2026-04-09T12:00:02.000Z",
            old="print('hi')",
            new="print('hello world')",
        ),
        # Edit 3: add docstring
        edit_entry(
            "edit-3", "edit-2", "2026-04-09T12:00:03.000Z",
            old="def greet():\n    print('hello world')",
            new="def greet():\n    \"\"\"Say hello.\"\"\"\n    print('hello world')",
        ),
    ]

    with path.open("w") as f:
        for entry in entries:
            f.write(_line(entry))

    return path


@pytest.fixture
def temp_store(tmp_path: Path):
    """Provide an isolated LonghandStore in a temp directory."""
    from longhand.storage import LonghandStore

    store = LonghandStore(data_dir=tmp_path / "longhand")
    yield store
