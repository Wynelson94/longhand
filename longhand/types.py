"""
Core data types for Longhand.

Every event in a Claude Code session becomes a typed Event.
Nothing is summarized — the raw JSON is preserved in `raw`.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    """The type of event recorded in a Claude Code session."""

    USER_MESSAGE = "user_message"
    ASSISTANT_TEXT = "assistant_text"
    ASSISTANT_THINKING = "assistant_thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_SNAPSHOT = "file_snapshot"
    SYSTEM = "system"
    SIDECHAIN = "sidechain"
    UNKNOWN = "unknown"


class FileOperation(str, Enum):
    """File operation types derived from tool calls."""

    READ = "read"
    WRITE = "write"
    EDIT = "edit"
    MULTI_EDIT = "multi_edit"
    DELETE = "delete"
    NOTEBOOK_EDIT = "notebook_edit"


class Event(BaseModel):
    """A single event in a Claude Code session.

    Every event is typed, timestamped, and preserves the raw JSON
    so nothing is ever lost.
    """

    event_id: str = Field(..., description="Unique event identifier (uuid from JSONL)")
    session_id: str = Field(..., description="Parent session identifier")
    parent_event_id: str | None = Field(None, description="Parent event (for tool result → tool call)")
    event_type: EventType
    sequence: int = Field(..., description="Order within the session")
    timestamp: datetime
    cwd: str | None = Field(None, description="Working directory at time of event")
    git_branch: str | None = None
    model: str | None = Field(None, description="Model that produced this event, if applicable")
    content: str = Field(default="", description="Primary searchable text content")
    is_sidechain: bool = False
    raw: dict[str, Any] = Field(default_factory=dict, description="Full original JSONL entry")

    # Tool-specific fields (populated for TOOL_CALL and TOOL_RESULT events)
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: str | None = None
    tool_success: bool | None = None

    # File operation fields (populated when tool_call touches a file)
    file_path: str | None = None
    file_operation: FileOperation | None = None
    old_content: str | None = None
    new_content: str | None = None

    # Error detection (populated on tool_result events during parse)
    error_detected: bool = False
    error_snippet: str | None = None
    error_category: str | None = None
    error_severity: str | None = None

    model_config = ConfigDict(use_enum_values=True)


class Session(BaseModel):
    """A Claude Code session — a collection of events belonging to one conversation."""

    session_id: str
    project_path: str | None = Field(None, description="Original project directory")
    transcript_path: str = Field(..., description="Path to the JSONL file on disk")
    started_at: datetime
    ended_at: datetime
    event_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0
    file_edit_count: int = 0
    git_branch: str | None = None
    cwd: str | None = None
    model: str | None = Field(None, description="Most frequent model used in session")


class FileState(BaseModel):
    """The reconstructed state of a file at a point in time within a session."""

    file_path: str
    session_id: str
    at_event_id: str
    at_timestamp: datetime
    content: str
    edits_applied: int
    source: str = Field(..., description="'write', 'edit_reconstruction', or 'read_snapshot'")


class SearchResult(BaseModel):
    """A single result from a search query."""

    event: Event
    score: float = Field(..., description="Relevance score (0-1)")
    snippet: str = Field(..., description="Highlighted content snippet")
