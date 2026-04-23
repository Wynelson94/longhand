"""
JSONL parser for Claude Code session files.

Reads the raw session files Claude Code writes to disk and produces
typed Event objects without losing any information. Every tool call,
every thinking block, every file edit is preserved verbatim.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from longhand.extractors.errors import detect_error
from longhand.extractors.git import extract_git_signal
from longhand.types import Event, EventType, FileOperation, Session

# Hard limits — keep the parser bounded so a malicious or corrupted JSONL
# can't crash or OOM the ingest pipeline.
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500MB per session file
MAX_LINE_LENGTH = 50 * 1024 * 1024        # 50MB per JSONL line


_MAX_UNIQUE_CWDS_SCANNED = 20


def _pick_best_project_cwd(events: list[Event]) -> str | None:
    """Pick the most-common cwd across events that resolves to a real project root.

    Excludes $HOME (Claude Code's default launch dir when no project is open) and
    any cwd that doesn't walk up to a project marker (.git, pyproject.toml, etc).
    Returns None if no such cwd exists — caller should fall back.
    """
    from longhand.analysis.project_inference import find_project_root_strict

    home_resolved: Path | None
    try:
        home_resolved = Path.home().resolve()
    except (OSError, PermissionError):
        home_resolved = None

    # Memo cwd-string → resolved-project-root (or None). Bounded: stop resolving
    # new cwds after _MAX_UNIQUE_CWDS_SCANNED, but keep counting occurrences of
    # cwds we've already resolved.
    resolved: dict[str, str | None] = {}
    counts: Counter[str] = Counter()

    for e in events:
        cwd = e.cwd
        if not cwd:
            continue

        if cwd not in resolved:
            if len(resolved) >= _MAX_UNIQUE_CWDS_SCANNED:
                continue
            resolved[cwd] = _resolve_cwd_to_project(cwd, home_resolved, find_project_root_strict)

        root = resolved[cwd]
        if root is not None:
            counts[root] += 1

    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _resolve_cwd_to_project(
    cwd: str,
    home_resolved: Path | None,
    find_root: Any,
) -> str | None:
    """Resolve a cwd string to the project root it belongs to, or None."""
    try:
        p = Path(cwd).resolve()
    except (OSError, PermissionError):
        return None
    if home_resolved is not None and p == home_resolved:
        return None
    if p.is_file():
        p = p.parent
    root = find_root(p)
    return str(root) if root is not None else None


FILE_EDIT_TOOLS = {
    "Edit": FileOperation.EDIT,
    "Write": FileOperation.WRITE,
    "MultiEdit": FileOperation.MULTI_EDIT,
    "NotebookEdit": FileOperation.NOTEBOOK_EDIT,
}

FILE_READ_TOOLS = {"Read"}

# Tools whose results can legitimately contain error output.
# Read tool output is usually source code — its contents shouldn't be scanned
# for errors because it may contain regex patterns, test fixtures, etc.
COMMAND_EXECUTING_TOOLS = {
    "Bash",
    "BashOutput",
    "KillShell",
    "run_command",
    "shell",
    "execute_command",
}


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
        # Bound the file size to prevent OOM on malicious or corrupted JSONLs
        try:
            size = self.file_path.stat().st_size
            if size > MAX_FILE_SIZE_BYTES:
                raise ValueError(
                    f"Session file exceeds {MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB limit: {file_path}"
                )
        except OSError:
            pass
        # Track tool_name and tool_input for each tool_use_id so we can gate
        # error detection and git extraction on tool_result events.
        self._tool_name_by_id: dict[str, str] = {}
        self._tool_input_by_id: dict[str, dict[str, Any]] = {}

    def parse_events(self) -> Iterator[Event]:
        """Yield Event objects from the session file, in file order.

        Deduplicates event_ids within a file. Claude Code subagents sometimes
        reuse the same uuid across streaming entries — when we detect a
        collision, we append a suffix so every event_id is unique.
        """
        sequence = 0
        seen_ids: dict[str, int] = {}
        with self.file_path.open("r", encoding="utf-8", errors="replace") as f:
            for _line_num, line in enumerate(f, start=1):
                # Skip lines that exceed the hard line-length limit
                if len(line) > MAX_LINE_LENGTH:
                    continue
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
                result_content = _stringify_tool_result(block.get("content"))
                tool_use_id = block.get("tool_use_id")
                paired_tool = self._tool_name_by_id.get(tool_use_id or "", "")

                # Only run error detection on results from command-executing tools.
                # Read/Glob/Grep/WebFetch output is usually source code or file
                # contents — it can legitimately contain strings that look like
                # errors (regex patterns, test fixtures, log lines in docs).
                error_signal = None
                git_signal = None
                if paired_tool in COMMAND_EXECUTING_TOOLS:
                    error_signal = detect_error(result_content)
                    # Extract structured git data from Bash git commands
                    paired_input = self._tool_input_by_id.get(tool_use_id or "", {})
                    git_signal = extract_git_signal(
                        command=paired_input.get("command", ""),
                        output=result_content,
                    )

                success_flag = (
                    tool_use_result.get("success") if isinstance(tool_use_result, dict) else None
                )
                has_error = bool(error_signal) or (success_flag is False)

                events.append(Event(
                    event_id=f"{event_id}:{offset}" if offset > 0 else event_id,
                    event_type=EventType.TOOL_RESULT,
                    sequence=base_sequence + offset,
                    content=result_content,
                    tool_use_id=tool_use_id,
                    tool_output=result_content,
                    tool_success=success_flag,
                    error_detected=has_error,
                    error_snippet=error_signal.snippet if error_signal else None,
                    error_category=error_signal.category if error_signal else None,
                    error_severity=error_signal.severity if error_signal else None,
                    git_operation=git_signal.operation_type if git_signal else None,
                    git_commit_hash=git_signal.commit_hash if git_signal else None,
                    git_commit_message=git_signal.commit_message if git_signal else None,
                    **common,
                ))
            else:
                # Plain user text message. Non-text blocks (images, any future
                # non-text type) are replaced with a semantic placeholder rather
                # than serialized as JSON — previously the full block was
                # json.dumps'd, which embedded base64 image payloads into the
                # event content and leaked them into segment keywords.
                if block_type == "text":
                    text = block.get("text", "")
                elif block_type == "image":
                    media_type = (block.get("source") or {}).get("media_type", "image")
                    text = f"[image: {media_type}]"
                else:
                    text = f"[{block_type}]" if block_type else ""
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

                # Remember the tool_name and input for this tool_use_id — the tool_result
                # that follows will look them up to gate error detection and git extraction.
                if tool_use_id:
                    self._tool_name_by_id[tool_use_id] = tool_name
                    self._tool_input_by_id[tool_use_id] = tool_input

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

        # For multi-project sessions the first-event cwd is often the shell's
        # launch dir (commonly $HOME), not where the work happened. Prefer the
        # most-common non-home cwd that resolves to a real project root.
        best_cwd = _pick_best_project_cwd(events) or first_real_cwd

        session_id = first_real_session or self.file_path.stem
        project_path = best_cwd or str(self.file_path.parent)
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

    Only top-level session transcripts are returned. Subagent JSONLs (under
    `*/subagents/`) and pytest temp-directory leftovers are filtered out —
    they aren't independent sessions and shouldn't be treated as such by
    ingestion or reconcile.
    """
    if claude_projects_dir is None:
        claude_projects_dir = Path.home() / ".claude" / "projects"
    else:
        claude_projects_dir = Path(claude_projects_dir)

    if not claude_projects_dir.exists():
        return []

    sessions: list[Path] = []
    for jsonl in claude_projects_dir.rglob("*.jsonl"):
        path_str = str(jsonl)
        # Skip internal plugin/skill files — they don't contain session events
        if "skill-injections" in jsonl.name or "vercel-plugin" in path_str:
            continue
        # Subagent transcripts live under .../<session-id>/subagents/<id>.jsonl.
        # They're referenced from the parent session's events, not standalone.
        if "/subagents/" in path_str:
            continue
        # Pytest leaves behind JSONLs in tmp project dirs during test runs.
        if "pytest-of-" in path_str:
            continue
        sessions.append(jsonl)

    return sessions
