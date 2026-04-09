"""
JSONL parser for Claude Code session files.

Reads the raw session files Claude Code writes to disk and produces
typed Event objects without losing any information. Every tool call,
every thinking block, every file edit is preserved verbatim.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from longhand.types import Event, EventType, FileOperation, Session


FILE_EDIT_TOOLS = {
    "Edit": FileOperation.EDIT,
    "Write": FileOperation.WRITE,
    "MultiEdit": FileOperation.MULTI_EDIT,
    "NotebookEdit": FileOperation.NOTEBOOK_EDIT,
}

FILE_READ_TOOLS = {"Read"}


def _parse_timestamp(value: str | None) -> datetime:
    """Parse an ISO timestamp, handling Claude Code's 'Z' suffix.

    Always returns a timezone-aware UTC datetime so comparisons work.
    """
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _extract_content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize message.content into a list of content blocks."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return []


def _stringify_tool_result(content: Any) -> str:
    """Tool result content can be str or a list of blocks — stringify it."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "content" in block:
                    parts.append(str(block["content"]))
                else:
                    parts.append(json.dumps(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _tool_input_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Produce a searchable string summary of a tool invocation.

    The full tool_input is always preserved in Event.tool_input.
    This is just for building semantic search text.
    """
    parts = [f"Tool: {tool_name}"]
    if not isinstance(tool_input, dict):
        return parts[0]

    priority_keys = [
        "file_path", "path", "command", "pattern", "query",
        "url", "description", "prompt", "old_string", "new_string",
        "content", "skill", "subagent_type",
    ]
    for key in priority_keys:
        if key in tool_input and tool_input[key]:
            value = str(tool_input[key])
            if len(value) > 500:
                value = value[:500] + "..."
            parts.append(f"{key}: {value}")

    return "\n".join(parts)


class JSONLParser:
    """Parse a Claude Code JSONL session file into Longhand Events."""

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        if not self.file_path.exists():
            raise FileNotFoundError(f"Session file not found: {file_path}")

    def parse_events(self) -> Iterator[Event]:
        """Yield Event objects from the session file, in file order.

        Deduplicates event_ids within a file. Claude Code subagents sometimes
        reuse the same uuid across streaming entries — when we detect a
        collision, we append a suffix so every event_id is unique.
        """
        sequence = 0
        seen_ids: dict[str, int] = {}
        with self.file_path.open("r", encoding="utf-8", errors="replace") as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # Skip corrupted lines rather than failing the whole parse
                    continue

                for event in self._entry_to_events(entry, sequence):
                    base_id = event.event_id
                    if base_id in seen_ids:
                        seen_ids[base_id] += 1
                        event.event_id = f"{base_id}#{seen_ids[base_id]}"
                    else:
                        seen_ids[base_id] = 0
                    yield event
                    sequence += 1

    def _entry_to_events(self, entry: dict[str, Any], base_sequence: int) -> list[Event]:
        """Convert one JSONL entry into zero or more Events.

        An assistant message with multiple content blocks (text, thinking,
        tool_use) produces one Event per block so each is independently
        searchable and timelineable.
        """
        entry_type = entry.get("type", "unknown")

        # File history snapshots — create a minimal event
        if entry_type == "file-history-snapshot":
            return [self._file_snapshot_event(entry, base_sequence)]

        # Queue operations and progress — skip (internal orchestration)
        if entry_type in {"queue-operation", "progress"}:
            return []

        # User messages
        if entry_type == "user":
            return self._parse_user_entry(entry, base_sequence)

        # Assistant messages
        if entry_type == "assistant":
            return self._parse_assistant_entry(entry, base_sequence)

        # System messages
        if entry_type == "system":
            return [self._system_event(entry, base_sequence)]

        # Unknown — preserve it anyway
        return [self._unknown_event(entry, base_sequence)]

    def _common_fields(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Extract fields that appear on most entry types."""
        return {
            "session_id": entry.get("sessionId", ""),
            "parent_event_id": entry.get("parentUuid"),
            "timestamp": _parse_timestamp(entry.get("timestamp")),
            "cwd": entry.get("cwd"),
            "git_branch": entry.get("gitBranch"),
            "is_sidechain": entry.get("isSidechain", False),
            "raw": entry,
        }

    def _parse_user_entry(self, entry: dict[str, Any], base_sequence: int) -> list[Event]:
        """A 'user' entry can be a plain message or a tool_result from the previous tool call."""
        message = entry.get("message", {})
        blocks = _extract_content_blocks(message)
        common = self._common_fields(entry)
        event_id = entry.get("uuid", f"user-{base_sequence}")

        events: list[Event] = []
        offset = 0

        # If the entry has toolUseResult metadata, treat the whole thing as a tool_result
        tool_use_result = entry.get("toolUseResult")

        for block in blocks:
            block_type = block.get("type")

            if block_type == "tool_result":
                events.append(Event(
                    event_id=f"{event_id}:{offset}" if offset > 0 else event_id,
                    event_type=EventType.TOOL_RESULT,
                    sequence=base_sequence + offset,
                    content=_stringify_tool_result(block.get("content")),
                    tool_use_id=block.get("tool_use_id"),
                    tool_output=_stringify_tool_result(block.get("content")),
                    tool_success=(
                        tool_use_result.get("success") if isinstance(tool_use_result, dict) else None
                    ),
                    **common,
                ))
            else:
                # Plain user text message
                text = block.get("text", "") if block_type == "text" else json.dumps(block)
                events.append(Event(
                    event_id=f"{event_id}:{offset}" if offset > 0 else event_id,
                    event_type=EventType.USER_MESSAGE,
                    sequence=base_sequence + offset,
                    content=text,
                    **common,
                ))
            offset += 1

        if not events:
            # User entry with no content blocks — still record it
            events.append(Event(
                event_id=event_id,
                event_type=EventType.USER_MESSAGE,
                sequence=base_sequence,
                content="",
                **common,
            ))

        return events

    def _parse_assistant_entry(self, entry: dict[str, Any], base_sequence: int) -> list[Event]:
        """Assistant entries contain a content list of text, thinking, and tool_use blocks."""
        message = entry.get("message", {})
        model = message.get("model")
        blocks = _extract_content_blocks(message)
        common = self._common_fields(entry)
        event_id = entry.get("uuid", f"asst-{base_sequence}")

        events: list[Event] = []
        offset = 0

        for block in blocks:
            block_type = block.get("type")
            sub_event_id = f"{event_id}:{offset}" if len(blocks) > 1 or offset > 0 else event_id

            if block_type == "text":
                text = block.get("text", "")
                if not text:
                    continue
                events.append(Event(
                    event_id=sub_event_id,
                    event_type=EventType.ASSISTANT_TEXT,
                    sequence=base_sequence + offset,
                    content=text,
                    model=model,
                    **common,
                ))

            elif block_type == "thinking":
                thinking = block.get("thinking", "")
                if not thinking:
                    continue
                events.append(Event(
                    event_id=sub_event_id,
                    event_type=EventType.ASSISTANT_THINKING,
                    sequence=base_sequence + offset,
                    content=thinking,
                    model=model,
                    **common,
                ))

            elif block_type == "tool_use":
                tool_name = block.get("name", "")
                tool_input = block.get("input", {}) or {}
                tool_use_id = block.get("id", "")

                file_path = None
                file_operation = None
                old_content = None
                new_content = None

                if tool_name in FILE_EDIT_TOOLS:
                    file_path = tool_input.get("file_path")
                    file_operation = FILE_EDIT_TOOLS[tool_name]
                    if tool_name == "Edit":
                        old_content = tool_input.get("old_string")
                        new_content = tool_input.get("new_string")
                    elif tool_name == "Write":
                        new_content = tool_input.get("content")
                    elif tool_name == "NotebookEdit":
                        old_content = tool_input.get("old_source")
                        new_content = tool_input.get("new_source")
                elif tool_name in FILE_READ_TOOLS:
                    file_path = tool_input.get("file_path")
                    file_operation = FileOperation.READ

                events.append(Event(
                    event_id=sub_event_id,
                    event_type=EventType.TOOL_CALL,
                    sequence=base_sequence + offset,
                    content=_tool_input_summary(tool_name, tool_input),
                    model=model,
                    tool_name=tool_name,
                    tool_use_id=tool_use_id,
                    tool_input=tool_input,
                    file_path=file_path,
                    file_operation=file_operation,
                    old_content=old_content,
                    new_content=new_content,
                    **common,
                ))

            offset += 1

        return events

    def _system_event(self, entry: dict[str, Any], base_sequence: int) -> Event:
        return Event(
            event_id=entry.get("uuid", f"sys-{base_sequence}"),
            event_type=EventType.SYSTEM,
            sequence=base_sequence,
            content=str(entry.get("content", entry.get("message", ""))),
            **self._common_fields(entry),
        )

    def _file_snapshot_event(self, entry: dict[str, Any], base_sequence: int) -> Event:
        return Event(
            event_id=entry.get("messageId", f"snap-{base_sequence}"),
            event_type=EventType.FILE_SNAPSHOT,
            sequence=base_sequence,
            content="",
            session_id=entry.get("sessionId", ""),
            parent_event_id=None,
            timestamp=_parse_timestamp(entry.get("snapshot", {}).get("timestamp")),
            cwd=None,
            git_branch=None,
            is_sidechain=False,
            raw=entry,
        )

    def _unknown_event(self, entry: dict[str, Any], base_sequence: int) -> Event:
        return Event(
            event_id=entry.get("uuid", f"unk-{base_sequence}"),
            event_type=EventType.UNKNOWN,
            sequence=base_sequence,
            content=json.dumps(entry)[:500],
            **self._common_fields(entry),
        )

    def build_session(self, events: list[Event] | None = None) -> Session:
        """Build a Session summary from the parsed events.

        If `events` isn't provided, the file is parsed again.
        """
        if events is None:
            events = list(self.parse_events())

        if not events:
            raise ValueError(f"No events found in session file: {self.file_path}")

        # Use the first event with a real session_id / cwd / git_branch —
        # file-history-snapshot events don't carry these.
        first_real_session = next((e.session_id for e in events if e.session_id), "")
        first_real_cwd = next((e.cwd for e in events if e.cwd), None)
        first_real_branch = next((e.git_branch for e in events if e.git_branch), None)

        session_id = first_real_session or self.file_path.stem
        project_path = first_real_cwd or str(self.file_path.parent)
        git_branch = first_real_branch

        # Ignore the epoch fallback when computing session boundaries
        epoch = datetime.fromtimestamp(0, tz=timezone.utc)
        real_timestamps = [e.timestamp for e in events if e.timestamp != epoch]
        started_at = min(real_timestamps) if real_timestamps else epoch
        ended_at = max(real_timestamps) if real_timestamps else epoch

        user_count = sum(1 for e in events if e.event_type == EventType.USER_MESSAGE.value)
        asst_count = sum(1 for e in events if e.event_type == EventType.ASSISTANT_TEXT.value)
        tool_count = sum(1 for e in events if e.event_type == EventType.TOOL_CALL.value)
        edit_count = sum(
            1 for e in events
            if e.event_type == EventType.TOOL_CALL.value
            and e.file_operation in {
                FileOperation.EDIT.value, FileOperation.WRITE.value,
                FileOperation.MULTI_EDIT.value, FileOperation.NOTEBOOK_EDIT.value,
            }
        )

        # Most frequent model
        models = [e.model for e in events if e.model]
        model = Counter(models).most_common(1)[0][0] if models else None

        return Session(
            session_id=session_id,
            project_path=project_path,
            transcript_path=str(self.file_path),
            started_at=started_at,
            ended_at=ended_at,
            event_count=len(events),
            user_message_count=user_count,
            assistant_message_count=asst_count,
            tool_call_count=tool_count,
            file_edit_count=edit_count,
            git_branch=git_branch,
            cwd=project_path,
            model=model,
        )


def discover_sessions(claude_projects_dir: str | Path | None = None) -> list[Path]:
    """Discover all Claude Code session JSONL files in a projects directory.

    Defaults to `~/.claude/projects` if no directory is provided.
    """
    if claude_projects_dir is None:
        claude_projects_dir = Path.home() / ".claude" / "projects"
    else:
        claude_projects_dir = Path(claude_projects_dir)

    if not claude_projects_dir.exists():
        return []

    sessions: list[Path] = []
    for jsonl in claude_projects_dir.rglob("*.jsonl"):
        # Skip internal plugin/skill files — they don't contain session events
        if "skill-injections" in jsonl.name or "vercel-plugin" in str(jsonl):
            continue
        sessions.append(jsonl)

    return sessions
