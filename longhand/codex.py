"""Codex rollout adapter: the same archive, the same rules, a second client.

Codex Desktop writes one JSONL "rollout" per thread under ``~/.codex/sessions``.
This module turns those records into Longhand events the way ``parser.py``
turns Claude Code transcripts into events — same Event model, same ID
discipline, same drift promise:

* ``response_item`` records are the canonical conversation (messages,
  reasoning summaries, tool calls and their outputs) and become events.
* ``event_msg`` records are Codex's UI stream. Every kind we know either
  mirrors a canonical item or is turn bookkeeping, so known kinds are skipped
  (a message is never stored twice) and UNKNOWN kinds are preserved as
  ``unknown`` events so ``longhand doctor`` can surface them.
* ``session_meta`` / ``turn_context`` carry cwd, model, and branch; they are
  stored as metadata ``system`` rows.
* Encrypted reasoning is opaque by design: a reasoning item with a summary
  becomes a thinking event; one without a summary is skipped, because
  ciphertext has no recall value.

Every stored event keeps its raw record. Session and tool IDs live in a
``codex:`` namespace so nothing collides with Claude records.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any

from longhand.extractors.errors import detect_error
from longhand.extractors.git import extract_git_signal
from longhand.types import Event, EventType

if TYPE_CHECKING:
    from longhand.storage.sqlite_store import SQLiteStore
    from longhand.types import Session

# Capture bounds. These are input bounds, not a memory ceiling: the default
# SQLite-only capture never loads the embedding model. Sized from real Codex
# Desktop rollouts, where 1–4 MB and a few hundred items per thread is normal.
DEFAULT_SESSION_LIMIT = 50
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_EVENTS = 20_000

# Top-level record types with nothing recallable in them — usage accounting
# and world-state snapshots. Skipped, not stored: the Claude parser's
# KNOWN_SKIP_ENTRY_TYPES rule. Every member needs a fixture line in
# tests/fixtures/codex_shapes/entries.jsonl.
CODEX_SKIP_RECORD_TYPES = frozenset({"token_usage_record", "world_state"})

# `event_msg` kinds that mirror something the canonical `response_item`
# stream already provides (user_message / agent_message / agent_reasoning
# duplicate messages and reasoning summaries; exec_command_end and
# patch_apply_end duplicate tool outputs) or are turn bookkeeping with no
# content (task_started, task_complete, item_completed, token_count,
# thread_settings_applied). Skipped so nothing lands twice. A kind that is
# NOT in this set is preserved as an unknown event — drift is never silent.
CODEX_SKIP_EVENT_MSG_TYPES = frozenset(
    {
        "agent_message",
        "agent_reasoning",
        "exec_command_end",
        "item_completed",
        "patch_apply_end",
        "task_complete",
        "task_started",
        "thread_settings_applied",
        "token_count",
        "user_message",
    }
)

# Record types the adapter routes: into events, into session metadata, or to
# a skip decision.
CODEX_HANDLED_RECORD_TYPES = frozenset(
    {"session_meta", "turn_context", "response_item", "event_msg"}
)
# `response_item` kinds the adapter turns into events.
CODEX_HANDLED_ITEM_TYPES = frozenset(
    {
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
    }
)

# `exec` is Codex Desktop's scripting tool: a JS snippet that calls
# tools.exec_command({cmd: "..."}) one or more times. The command literals
# are pulled out so error suppression and git detection see the real command.
_JS_CMD_LITERAL = re.compile(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")


def discover_codex_sessions(codex_home: str | Path | None = None) -> list[Path]:
    """Every rollout under the Codex home (`CODEX_HOME`, default `~/.codex`)."""
    root = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    found: set[Path] = set()
    for name in ("sessions", "archived_sessions"):
        folder = root / name
        if folder.is_dir():
            found.update(folder.rglob("*.jsonl"))
    return sorted(found)


def read_session_meta(path: Path) -> dict[str, Any] | None:
    """The `session_meta` payload from a rollout's first line, or None.

    Reads one bounded line, so it is cheap enough to call on every rollout
    during discovery.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            line = f.readline(1024 * 1024)
        entry = json.loads(line)
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict) or entry.get("type") != "session_meta":
        return None
    payload = entry.get("payload")
    return payload if isinstance(payload, dict) else None


def is_subagent_rollout(meta: dict[str, Any] | None) -> bool:
    """Whether a rollout is a thread Codex spawned for itself.

    Codex marks those — e.g. the "guardian" approval reviewer — with a
    structured `source` ({"subagent": {...}}) where a user-driven thread has
    the client name ("vscode", "cli"). They re-quote the parent thread, so
    capturing them by default would store every conversation twice.
    """
    if not meta:
        return False
    source = meta.get("source")
    return isinstance(source, dict) and "subagent" in source


def shell_commands_from_tool_input(tool_input: dict[str, Any] | None) -> str:
    """The shell command(s) a Codex tool call ran, newline-joined; "" if none.

    Direct shell tools carry `cmd` (a string or an argv list). The `exec`
    scripting tool embeds `cmd: "..."` literals inside JS, which are decoded
    with JSON escaping rules (the common subset of JS string escapes).
    """
    if not tool_input:
        return ""
    command = tool_input.get("cmd", tool_input.get("command"))
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    if isinstance(command, str):
        return command
    script = tool_input.get("input")
    if not isinstance(script, str):
        return ""
    commands: list[str] = []
    for match in _JS_CMD_LITERAL.finditer(script):
        literal = match.group(1)
        inner = literal[1:-1]
        try:
            if literal[0] == '"':
                commands.append(json.loads(literal))
            else:
                rewrapped = inner.replace("\\'", "'").replace('"', '\\"')
                commands.append(json.loads(f'"{rewrapped}"'))
        except ValueError:
            commands.append(inner)
    return "\n".join(commands)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"input_text", "output_text", "text", "summary_text"}:
            if isinstance(block.get("text"), str):
                parts.append(block["text"])
        elif block.get("type") in {"input_image", "image"}:
            parts.append("[image]")
    return "\n".join(parts)


def disposition(entry: dict[str, Any]) -> str:
    """How the adapter treats one raw record: "handled", "skipped", or "unknown".

    tests/test_codex_shapes.py asserts every fixture line is handled or
    skipped; "unknown" is what doctor's drift row reports on a live archive.
    """
    record_type = entry.get("type")
    payload = entry.get("payload")
    kind = payload.get("type") if isinstance(payload, dict) else None
    if record_type in CODEX_SKIP_RECORD_TYPES:
        return "skipped"
    if record_type == "event_msg":
        return "skipped" if kind in CODEX_SKIP_EVENT_MSG_TYPES else "unknown"
    if record_type == "response_item":
        return "handled" if kind in CODEX_HANDLED_ITEM_TYPES else "unknown"
    if record_type in CODEX_HANDLED_RECORD_TYPES:
        return "handled"
    return "unknown"


class CodexAdapter:
    """Stateful conversion of one complete rollout, in append order."""

    def __init__(self, metadata: dict[str, Any]):
        identity = metadata.get("id") or metadata.get("session_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Codex session_meta is missing its session id")
        self.session_id = f"codex:{identity}"
        self.cwd = metadata.get("cwd")
        git = metadata.get("git") or {}
        self.branch = git.get("branch") if isinstance(git, dict) else None
        self.model = None
        self.calls: dict[str, Event] = {}

    def convert(self, entry: dict[str, Any], sequence: int) -> list[Event]:
        from longhand.parser import _parse_timestamp

        record_type = entry.get("type")
        payload = entry.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        kind = payload.get("type")

        if record_type in CODEX_SKIP_RECORD_TYPES:
            return []
        if record_type == "event_msg" and kind in CODEX_SKIP_EVENT_MSG_TYPES:
            return []
        if record_type == "turn_context":
            self.cwd = payload.get("cwd", self.cwd)
            self.model = payload.get("model", self.model)

        event = Event(
            event_id=f"{self.session_id}:{sequence}",
            session_id=self.session_id,
            parent_event_id=None,
            event_type=EventType.SYSTEM,
            sequence=sequence,
            timestamp=_parse_timestamp(entry.get("timestamp")),
            cwd=self.cwd,
            git_branch=self.branch,
            model=self.model,
            raw=entry,
        )
        if record_type in ("session_meta", "turn_context"):
            return [event]
        if record_type != "response_item" or kind not in CODEX_HANDLED_ITEM_TYPES:
            # An undispositioned shape: keep it, raw intact, where doctor's
            # drift row can see it — the same path the Claude parser takes.
            event.event_type = EventType.UNKNOWN
            event.content = json.dumps(entry)[:500]
            return [event]

        if kind == "message":
            role = payload.get("role")
            if role == "user":
                event.event_type = EventType.USER_MESSAGE
            elif role == "assistant":
                event.event_type = EventType.ASSISTANT_TEXT
            # Instructions (developer/system roles) stay in raw, rather than
            # polluting recall results.
            if event.event_type != EventType.SYSTEM:
                event.content = _text(payload.get("content"))
        elif kind == "reasoning":
            summary = _text(payload.get("summary"))
            if not summary:
                return []  # encrypted-only reasoning: nothing readable to keep
            event.event_type = EventType.ASSISTANT_THINKING
            event.content = summary
        elif kind in {"function_call", "custom_tool_call"}:
            event.event_type = EventType.TOOL_CALL
            event.tool_name = payload.get("name")
            call_id = payload.get("call_id")
            event.tool_use_id = f"{self.session_id}:{call_id}" if call_id else None
            tool_input: dict[str, Any]
            if kind == "function_call":
                arguments = payload.get("arguments", {})
                try:
                    arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                except ValueError:
                    arguments = {"arguments": arguments}
                tool_input = arguments if isinstance(arguments, dict) else {"arguments": arguments}
            else:
                tool_input = {"input": payload.get("input", "")}
            # When the shell command had to be dug out of an `exec` script,
            # surface it under the key the rest of Longhand reads, so git-ops
            # re-extraction and error suppression see it too.
            command = shell_commands_from_tool_input(tool_input)
            if command and "cmd" not in tool_input and "command" not in tool_input:
                tool_input["command"] = command
            event.tool_input = tool_input
            event.content = f"Tool: {event.tool_name}\n" + json.dumps(
                tool_input, ensure_ascii=False
            )
            if event.tool_use_id:
                self.calls[event.tool_use_id] = event
        else:  # function_call_output / custom_tool_call_output
            event.event_type = EventType.TOOL_RESULT
            call_id = payload.get("call_id")
            event.tool_use_id = f"{self.session_id}:{call_id}" if call_id else None
            output = payload.get("output", "")
            event.content = (
                output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            )
            event.tool_output = event.content
            call = self.calls.get(event.tool_use_id or "")
            if call:
                event.parent_event_id = call.event_id
                event.tool_name = call.tool_name
                # Same gate as the Claude parser: signals come from
                # command-executing calls, with the command as context so
                # probe/search noise is suppressed at the source.
                command = shell_commands_from_tool_input(call.tool_input)
                if command:
                    signal = detect_error(event.content, tool_name=call.tool_name, command=command)
                    if signal:
                        event.error_detected = True
                        event.error_snippet = signal.snippet
                        event.error_category = signal.category
                        event.error_severity = signal.severity
                    git = extract_git_signal(command, event.content)
                    if git:
                        event.git_operation = git.operation_type
                        event.git_commit_hash = git.commit_hash
                        event.git_commit_message = git.commit_message
        return [event]


class CodexArchiveStore:
    """Exact-record capture into the shared SQLite archive — no Chroma, no model.

    Stage `archived` marks a session whose raw capture is complete and whose
    semantic analysis has deliberately not run; `longhand codex-sync
    --semantic` upgrades it in place.
    """

    completed_stage = "archived"

    def __init__(
        self, data_dir: str | Path | None = None, sqlite: SQLiteStore | None = None
    ) -> None:
        from longhand.storage.sqlite_store import SQLiteStore
        from longhand.storage.store import resolve_data_dir

        self.data_dir = resolve_data_dir(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sqlite = sqlite if sqlite is not None else SQLiteStore(self.data_dir / "longhand.db")

    def ingest_session(self, session: Session, events: list[Event]) -> None:
        from longhand.storage.store import LonghandStore

        self.sqlite.mark_ingest_started(session.transcript_path, session.session_id)
        self.sqlite.upsert_session(session)
        self.sqlite.insert_events(events)
        self.sqlite.upsert_tool_pairs(self.sqlite.build_tool_pairs_from_events(events))
        # Commits made from Codex show up in find_commits / git-log on this
        # path too: the extraction is pure text work, no vectors involved.
        self.sqlite.insert_git_operations(
            LonghandStore._extract_git_operations(session.session_id, events)
        )
        self.sqlite.log_ingestion(
            session.transcript_path,
            session.session_id,
            Path(session.transcript_path).stat().st_size,
            len(events),
        )
        self.sqlite.set_analysis_stage(session.transcript_path, self.completed_stage)


@dataclass
class CodexScan:
    """Discovery result: which rollouts need capture, and why the rest don't."""

    candidates: list[Path] = field(default_factory=list)  # new or changed, within bounds
    unchanged: list[Path] = field(default_factory=list)  # captured at this exact size
    subagents: list[Path] = field(default_factory=list)  # skipped unless include_subagents
    oversize: list[Path] = field(default_factory=list)  # over max_file_bytes

    @property
    def on_disk(self) -> int:
        return len(self.candidates) + len(self.unchanged) + len(self.subagents) + len(self.oversize)


def scan_codex_sessions(
    sqlite: SQLiteStore,
    codex_home: str | Path | None = None,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    include_subagents: bool = False,
    complete_stages: tuple[str, ...] = ("archived", "analyzed"),
) -> CodexScan:
    """Classify every rollout on disk against the archive without parsing any."""
    scan = CodexScan()
    stages = sqlite.analysis_stages()
    for path in discover_codex_sessions(codex_home):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        key = str(path)
        if sqlite.already_ingested(key, size) and stages.get(key) in complete_stages:
            scan.unchanged.append(path)
        elif not include_subagents and is_subagent_rollout(read_session_meta(path)):
            scan.subagents.append(path)
        elif size > max_file_bytes:
            scan.oversize.append(path)
        else:
            scan.candidates.append(path)
    return scan


def sync_codex(
    store: Any,
    codex_home: str | Path | None = None,
    *,
    limit: int = DEFAULT_SESSION_LIMIT,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_events: int = DEFAULT_MAX_EVENTS,
    include_subagents: bool = False,
    claim_lock: bool = True,
) -> dict[str, Any]:
    """Import new or changed rollouts into the archive.

    `store` is a CodexArchiveStore (exact records only) or a LonghandStore
    (`--semantic`: the full pipeline, vectors included). Runs under Longhand's
    ingest lock; pass `claim_lock=False` when the caller already holds it, as
    reconcile does. This is deliberately a separate discovery path: Codex
    support never changes which files Claude's hooks scan.

    Report keys: ingested, skipped (unchanged), skipped_subagents, deferred
    (over a bound, or past `limit` for this run), errors, locked.
    """
    from longhand.parser import JSONLParser
    from longhand.recall.project_fallback import claim_ingest_lock, release_ingest_lock

    if min(limit, max_file_bytes, max_events) < 1:
        raise ValueError("Sync limits must be positive")
    report: dict[str, Any] = {
        "ingested": 0,
        "skipped": 0,
        "skipped_subagents": 0,
        "deferred": [],
        "errors": [],
        "locked": False,
    }
    if claim_lock and not claim_ingest_lock(store):
        report["locked"] = True
        return report
    try:
        complete: tuple[str, ...] = (
            ("archived", "analyzed") if isinstance(store, CodexArchiveStore) else ("analyzed",)
        )
        scan = scan_codex_sessions(
            store.sqlite,
            codex_home,
            max_file_bytes=max_file_bytes,
            include_subagents=include_subagents,
            complete_stages=complete,
        )
        report["skipped"] = len(scan.unchanged)
        report["skipped_subagents"] = len(scan.subagents)
        report["deferred"].extend(str(p) for p in scan.oversize)
        for index, path in enumerate(scan.candidates):
            if index >= limit:
                report["deferred"].append(str(path))
                continue
            try:
                size = path.stat().st_size
                parser = JSONLParser(path)
                events = list(islice(parser.parse_events(), max_events + 1))
                if len(events) > max_events or path.stat().st_size > max_file_bytes:
                    report["deferred"].append(str(path))
                    continue
                if not events:
                    continue
                session = parser.build_session(events)
                if not session.session_id.startswith("codex:"):
                    raise ValueError("Not a Codex rollout (missing session_meta)")
                store.ingest_session(session, events)
                # Record the size that was parsed, not whatever the file grew
                # to while embedding ran, so appended bytes are seen next scan.
                with store.sqlite.connect() as conn:
                    conn.execute(
                        "UPDATE ingestion_log SET file_size = ? WHERE transcript_path = ?",
                        (size, str(path)),
                    )
                report["ingested"] += 1
            except Exception as exc:
                report["errors"].append(
                    {"path": str(path), "error": f"{type(exc).__name__}: {exc}"}
                )
    finally:
        if claim_lock:
            release_ingest_lock(store)
    return report
