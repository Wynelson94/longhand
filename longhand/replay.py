"""
Session replay and file state reconstruction.

Given a session and a file, reconstruct what that file looked like
at any point in the session by applying edits in sequence.

This is the killer feature. Summary-based memory systems cannot do this.
Longhand can, because every edit's old_string and new_string is preserved
verbatim in the original session file.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from longhand.storage.sqlite_store import SQLiteStore
from longhand.types import FileState


def _apply_edit(content: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """Apply a single Edit tool call to a content string.

    Matches Claude Code's Edit semantics:
    - If replace_all is True, replaces every occurrence
    - Otherwise replaces only the first occurrence
    - If old_string is not found, returns content unchanged
    """
    if not old_string:
        return content
    if replace_all:
        return content.replace(old_string, new_string)
    idx = content.find(old_string)
    if idx < 0:
        return content
    return content[:idx] + new_string + content[idx + len(old_string) :]


def _apply_multi_edit(content: str, edits: list[dict[str, Any]]) -> str:
    """Apply a MultiEdit tool call — sequential edits."""
    result = content
    for edit in edits:
        old_s = edit.get("old_string", "")
        new_s = edit.get("new_string", "")
        replace_all = edit.get("replace_all", False)
        result = _apply_edit(result, old_s, new_s, replace_all)
    return result


class ReplayEngine:
    """Reconstructs file state at any point in a session."""

    def __init__(self, store: SQLiteStore):
        self.store = store

    def file_state_at(
        self,
        file_path: str,
        session_id: str,
        at_timestamp: datetime | None = None,
        at_event_id: str | None = None,
    ) -> FileState | None:
        """Reconstruct the state of `file_path` at a specific point.

        If neither timestamp nor event_id is given, returns the state at the
        end of the session (after all edits).
        """
        edits = self.store.get_file_edits(file_path, session_id=session_id)
        if not edits:
            # Maybe we only have a Read of this file — try to return that snapshot
            read_events = self.store.get_events(
                session_id=session_id,
                file_path=file_path,
                limit=10,
            )
            for e in read_events:
                if e.get("tool_name") == "Read":
                    # Read tool has file content in tool_output (we stored it)
                    output = e.get("tool_output") or ""
                    if output:
                        return FileState(
                            file_path=file_path,
                            session_id=session_id,
                            at_event_id=e["event_id"],
                            at_timestamp=datetime.fromisoformat(e["timestamp"]),
                            content=output,
                            edits_applied=0,
                            source="read_snapshot",
                        )
            return None

        # Find the starting content: either a Write (which sets full content)
        # or, if we start from an Edit, we need to reconstruct from old_string.
        content: str | None = None
        start_index = 0

        # Walk forward from the beginning to find the most recent Write that
        # precedes or equals the target. A Write is an authoritative full-content reset.
        target_sequence = self._target_sequence(edits, at_timestamp, at_event_id)

        write_anchor: dict[str, Any] | None = None
        for i, edit in enumerate(edits):
            if edit.get("sequence", 0) > target_sequence:
                break
            if edit.get("tool_name") == "Write":
                content = edit.get("new_content") or ""
                start_index = i + 1
                write_anchor = edit

        if content is None:
            # No Write before the target — reconstruct forward from first Edit's old_string
            first_edit = edits[0]
            content = first_edit.get("old_content") or ""
            start_index = 0

        # The Write anchor counts as one applied operation
        edits_applied = 1 if write_anchor is not None else 0
        last_event: dict[str, Any] | None = write_anchor

        for edit in edits[start_index:]:
            if edit.get("sequence", 0) > target_sequence:
                break

            last_event = edit
            tool_name = edit.get("tool_name")

            if tool_name == "Edit":
                import json as _json
                tool_input_raw = edit.get("tool_input_json")
                replace_all = False
                if tool_input_raw:
                    try:
                        ti = _json.loads(tool_input_raw)
                        replace_all = bool(ti.get("replace_all", False))
                    except Exception:
                        pass
                content = _apply_edit(
                    content,
                    edit.get("old_content") or "",
                    edit.get("new_content") or "",
                    replace_all=replace_all,
                )
                edits_applied += 1

            elif tool_name == "Write":
                content = edit.get("new_content") or ""
                edits_applied += 1

            elif tool_name == "MultiEdit":
                import json as _json
                tool_input_raw = edit.get("tool_input_json")
                if tool_input_raw:
                    try:
                        ti = _json.loads(tool_input_raw)
                        content = _apply_multi_edit(content, ti.get("edits", []))
                        edits_applied += 1
                    except Exception:
                        pass

        if last_event is None:
            return None

        return FileState(
            file_path=file_path,
            session_id=session_id,
            at_event_id=last_event["event_id"],
            at_timestamp=datetime.fromisoformat(last_event["timestamp"]),
            content=content or "",
            edits_applied=edits_applied,
            source="edit_reconstruction",
        )

    def _target_sequence(
        self,
        edits: list[dict[str, Any]],
        at_timestamp: datetime | None,
        at_event_id: str | None,
    ) -> int:
        """Determine the sequence number cutoff for replay."""
        if at_event_id:
            for e in edits:
                if e["event_id"] == at_event_id:
                    return e["sequence"]
            # If not in edits, look it up in the general events table
            row = self.store.get_event(at_event_id)
            if row:
                return row["sequence"]
            return 10**12  # unreachable — replay through all edits

        if at_timestamp:
            cutoff = -1
            for e in edits:
                ts = datetime.fromisoformat(e["timestamp"])
                if ts <= at_timestamp:
                    cutoff = e["sequence"]
                else:
                    break
            return cutoff if cutoff >= 0 else 10**12

        # Default: end of session
        return 10**12

    def file_history(self, file_path: str, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return every edit that touched a file, in chronological order."""
        return self.store.get_file_edits(file_path, session_id=session_id)

    def diff_edit(self, event_id: str) -> dict[str, str] | None:
        """Return the old/new content for a single edit event."""
        row = self.store.get_event(event_id)
        if not row or row.get("event_type") != "tool_call":
            return None
        return {
            "tool_name": row.get("tool_name") or "",
            "file_path": row.get("file_path") or "",
            "old": row.get("old_content") or "",
            "new": row.get("new_content") or "",
        }
