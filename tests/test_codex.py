"""Cross-client archive regressions, using synthetic Codex rollout records."""

import json
from unittest.mock import MagicMock

import pytest

from longhand.codex import discover_codex_sessions, sync_codex
from longhand.parser import JSONLParser
from longhand.storage.sqlite_store import SQLiteStore


def record(kind, payload):
    return {"timestamp": "2026-09-07T12:00:00Z", "type": kind, "payload": payload}


def write_rollout(path, identity="test-id", source=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": identity, "cwd": "/tmp/project", "git": {"branch": "main"}}
    if source is not None:
        meta["source"] = source
    entries = [
        record("session_meta", meta),
        record("turn_context", {"cwd": "/tmp/project", "model": "test-model"}),
        record("event_msg", {"type": "user_message", "message": "Fix the login"}),
        record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Fix the login"},
                    {"type": "input_image", "image_url": "data:image/png;base64,SECRET_IMAGE"},
                ],
            },
        ),
        record(
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "Recorded summary"}],
                "encrypted_content": "OPAQUE_CIPHERTEXT",
            },
        ),
        record(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": json.dumps({"cmd": "git commit -m 'Fix login'"}),
            },
        ),
        record(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "[main abc1234] Fix login\n 1 file changed",
            },
        ),
        record(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "call-2",
                "input": "text(await tools.exec_command({cmd: 'pwd'}));",
            },
        ),
        record(
            "response_item",
            {"type": "custom_tool_call_output", "call_id": "call-2", "output": "project"},
        ),
        record(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Fixed login"}],
            },
        ),
        record("future_record", {"type": "new-kind", "new_data": True}),
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return entries


def test_codex_preserves_raw_and_avoids_duplicate_messages(tmp_path):
    path = tmp_path / "rollout.jsonl"
    entries = write_rollout(path)
    parser = JSONLParser(path)
    events = list(parser.parse_events())
    # The event_msg mirror of the user message is skipped, not stored twice;
    # everything else keeps its raw record, including the undispositioned
    # future_record, which lands as a preserved unknown event.
    assert [e.raw for e in events] == [e for e in entries if e["type"] != "event_msg"]
    assert sum(e.event_type == "user_message" for e in events) == 1
    assert events[2].content == "Fix the login\n[image]"
    assert events[3].content == "Recorded summary"
    assert "OPAQUE_CIPHERTEXT" not in "".join(e.content for e in events)
    assert "SECRET_IMAGE" not in "".join(e.content for e in events)
    session = parser.build_session(events)
    assert session.session_id == "codex:test-id"
    assert session.model == "test-model"
    assert session.tool_call_count == 2
    assert events[5].git_commit_hash == "abc1234"
    assert events[5].parent_event_id == events[4].event_id
    assert events[5].tool_use_id == events[4].tool_use_id
    assert events[6].tool_input["input"].startswith("text(await")
    assert events[6].tool_input["command"] == "pwd"
    assert events[-1].event_type == "unknown"
    assert events[-1].raw["type"] == "future_record"


def test_append_and_reparse_ids_are_stable(tmp_path):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    parser = JSONLParser(path)
    before = list(parser.parse_events())
    with path.open("a") as f:
        f.write(
            json.dumps(
                record("response_item", {"type": "message", "role": "user", "content": "Next task"})
            )
            + "\n"
        )
    after = list(parser.parse_events())
    assert [e.event_id for e in before] == [e.event_id for e in after[:-1]]
    other = tmp_path / "other.jsonl"
    write_rollout(other, "other-id")
    other_events = list(JSONLParser(other).parse_events())
    assert {e.event_id for e in before}.isdisjoint(e.event_id for e in other_events)
    assert before[5].tool_use_id != other_events[5].tool_use_id


def test_codex_and_claude_share_sqlite_without_collisions(tmp_path, sample_session_file):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path, "test-session-1")
    store = SQLiteStore(tmp_path / "archive.db")
    for source in (sample_session_file, path, path):
        parser = JSONLParser(source)
        events = list(parser.parse_events())
        store.upsert_session(parser.build_session(events))
        store.insert_events(events)
    assert len(store.list_sessions()) == 2
    assert len(store.get_events(session_id="codex:test-session-1")) == 10
    assert store.get_events(session_id="test-session-1")


def test_discover_active_archived_and_custom_home(tmp_path, monkeypatch):
    active = tmp_path / "sessions/2026/09/07/rollout.jsonl"
    archived = tmp_path / "archived_sessions/archived.jsonl"
    write_rollout(active)
    write_rollout(archived, "archived")
    (tmp_path / "unrelated.jsonl").write_text("{}\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert set(discover_codex_sessions()) == {active, archived}


def test_sync_skips_unchanged_and_retries_append(tmp_path, monkeypatch):
    source = tmp_path / "codex/sessions/rollout.jsonl"
    write_rollout(source)
    store = MagicMock()
    store.data_dir = tmp_path / "archive"
    store.data_dir.mkdir()
    store.sqlite = SQLiteStore(store.data_dir / "longhand.db")

    def ingest(session, events):
        store.sqlite.upsert_session(session)
        store.sqlite.insert_events(events)
        store.sqlite.log_ingestion(
            session.transcript_path, session.session_id, source.stat().st_size, len(events)
        )
        store.sqlite.set_analysis_stage(session.transcript_path, "analyzed")

    store.ingest_session.side_effect = ingest
    assert sync_codex(store, tmp_path / "codex")["ingested"] == 1
    assert sync_codex(store, tmp_path / "codex")["skipped"] == 1
    assert store.sqlite.analysis_stages()[str(source)] == "analyzed"
    # A failure after the ingestion log is written must be retried even
    # when the transcript's byte count has not changed.
    store.sqlite.set_analysis_stage(str(source), "pending")
    assert sync_codex(store, tmp_path / "codex")["ingested"] == 1
    assert store.sqlite.analysis_stages()[str(source)] == "analyzed"
    with source.open("a") as f:
        f.write(
            json.dumps(
                record(
                    "response_item",
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Anything else?"}],
                    },
                )
            )
            + "\n"
        )
    assert sync_codex(store, tmp_path / "codex")["ingested"] == 1
    assert len(store.sqlite.get_events(session_id="codex:test-id")) == 11


def test_sync_honors_existing_writer_lock(tmp_path, monkeypatch):
    monkeypatch.setattr("longhand.recall.project_fallback.claim_ingest_lock", lambda _: False)
    store = MagicMock()
    assert sync_codex(store, tmp_path)["locked"]
    store.ingest_session.assert_not_called()


def test_codex_redaction_applies_to_raw_and_searchable_text(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    secret = "ghp_" + "a" * 36
    with path.open("a") as f:
        f.write(
            json.dumps(
                record("response_item", {"type": "message", "role": "user", "content": secret})
            )
            + "\n"
        )
    monkeypatch.setattr("longhand.parser.redaction_enabled", lambda: True)
    events = list(JSONLParser(path).parse_events())
    assert secret not in events[-1].content
    assert secret not in json.dumps(events[-1].raw)


def test_missing_metadata_identity_is_reported(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(record("session_meta", {})) + "\n")
    with pytest.raises(ValueError, match="session id"):
        list(JSONLParser(path).parse_events())


def test_codex_git_details_survive_storage_extraction(tmp_path):
    from longhand.storage.store import LonghandStore

    path = tmp_path / "rollout.jsonl"
    write_rollout(path)
    events = list(JSONLParser(path).parse_events())
    operations = LonghandStore._extract_git_operations("codex:test-id", events)
    assert len(operations) == 1
    assert operations[0]["files_changed_count"] == 1
    assert operations[0]["commit_hash"] == "abc1234"


def test_sync_bounds_work_before_ingestion(tmp_path):
    source = tmp_path / "codex/sessions/one.jsonl"
    write_rollout(source)
    write_rollout(source.with_name("two.jsonl"), "two")
    store = MagicMock()
    store.data_dir = tmp_path / "archive"
    store.data_dir.mkdir()
    store.sqlite = SQLiteStore(store.data_dir / "longhand.db")
    report = sync_codex(store, tmp_path / "codex", max_file_bytes=1)
    assert len(report["deferred"]) == 2
    store.ingest_session.assert_not_called()
    report = sync_codex(store, tmp_path / "codex", max_events=2)
    assert len(report["deferred"]) == 2
    store.ingest_session.assert_not_called()
    # The per-run limit defers the rest rather than dropping them.
    report = sync_codex(store, tmp_path / "codex", limit=1)
    assert report["ingested"] == 1
    assert len(report["deferred"]) == 1
    assert store.ingest_session.call_count == 1


def test_vector_collections_share_cpu_model_without_loading_it(tmp_path, monkeypatch):
    from longhand.storage import vector_store

    client = MagicMock()
    factory = MagicMock(return_value=client)
    embedding = MagicMock()
    monkeypatch.setattr(vector_store.chromadb, "PersistentClient", factory)
    monkeypatch.setattr(vector_store, "ONNXMiniLM_L6_V2", embedding)
    vector_store.VectorStore(tmp_path)
    embedding.assert_called_once_with(preferred_providers=["CPUExecutionProvider"])
    assert client.get_or_create_collection.call_count == 5
    for call in client.get_or_create_collection.call_args_list:
        assert call.kwargs["embedding_function"] is embedding.return_value
    assert vector_store.CHROMA_BATCH_SIZE == 500


def test_archive_only_and_lightweight_search(tmp_path, sample_session_file, monkeypatch):
    from longhand import lightweight_mcp
    from longhand.codex import CodexArchiveStore

    source = tmp_path / "codex/sessions/one.jsonl"
    write_rollout(source)
    store = CodexArchiveStore(tmp_path / "archive")
    # Embedding construction must never run on the archive-only path.
    monkeypatch.setattr(
        "longhand.storage.store.VectorStore", lambda *a, **k: pytest.fail("loaded vectors")
    )
    assert sync_codex(store, tmp_path / "codex")["ingested"] == 1
    assert sync_codex(store, tmp_path / "codex")["skipped"] == 1
    parser = JSONLParser(sample_session_file)
    events = list(parser.parse_events())
    store.ingest_session(parser.build_session(events), events)
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(store.data_dir))
    sessions = lightweight_mcp.list_sessions()
    assert len(sessions) == 2
    assert [s["session_id"] for s in lightweight_mcp.list_sessions(source="codex")] == [
        "codex:test-id"
    ]
    assert len(lightweight_mcp.list_sessions(source="claude")) == 1
    assert all(
        "/tmp/project" in s["project_path"]
        for s in lightweight_mcp.list_sessions(project="/tmp/project")
    )
    assert lightweight_mcp.list_sessions(project="no-such-project-anywhere") == []
    with pytest.raises(ValueError, match="codex"):
        lightweight_mcp.list_sessions(source="gemini")
    # Commits made from Codex reach the git_operations table on this path too.
    with store.sqlite.connect() as conn:
        commits = conn.execute(
            "SELECT commit_hash FROM git_operations WHERE session_id = ?", ("codex:test-id",)
        ).fetchall()
    assert [row[0] for row in commits] == ["abc1234"]
    hits = lightweight_mcp.search("Fix the login", session_id="codex:test-id")
    assert len(hits) == 1
    assert lightweight_mcp.search("%", session_id="codex:test-id") == []
    first = lightweight_mcp.get_event_text(hits[0]["event_id"])
    assert first["text"] == "Fix the login\n[image]"
    assert (
        lightweight_mcp.get_event_text(hits[0]["event_id"], offset=4)["text"] == first["text"][4:]
    )
    raw = lightweight_mcp.get_event_text(hits[0]["event_id"], raw=True)
    assert json.loads(raw["text"])["payload"]["role"] == "user"
    assert len(lightweight_mcp.get_session_timeline("codex:test-id", limit=2)) == 2


def test_sync_skips_subagent_threads_unless_asked(tmp_path):
    from longhand.codex import CodexArchiveStore

    home = tmp_path / "codex"
    write_rollout(home / "sessions/primary.jsonl", "primary")
    write_rollout(
        home / "sessions/guardian.jsonl", "guardian", source={"subagent": {"other": "guardian"}}
    )
    store = CodexArchiveStore(tmp_path / "archive")

    report = sync_codex(store, home)
    assert report["ingested"] == 1
    assert report["skipped_subagents"] == 1
    assert [s["session_id"] for s in store.sqlite.list_sessions()] == ["codex:primary"]

    report = sync_codex(store, home, include_subagents=True)
    assert report["ingested"] == 1
    assert report["skipped_subagents"] == 0
    assert {s["session_id"] for s in store.sqlite.list_sessions()} == {
        "codex:primary",
        "codex:guardian",
    }


def test_reconcile_captures_codex_rollouts_under_its_own_lock(temp_store, tmp_path, monkeypatch):
    from longhand.recall import reconcile as reconcile_mod
    from longhand.recall.project_fallback import claim_ingest_lock, release_ingest_lock
    from longhand.recall.reconcile import run_reconcile

    # No Claude transcripts on "disk": this is the Codex-only path, and it
    # must never walk a developer's real ~/.claude/projects.
    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [])
    home = tmp_path / "codex"
    write_rollout(home / "sessions/primary.jsonl", "primary")
    write_rollout(
        home / "sessions/guardian.jsonl", "guardian", source={"subagent": {"other": "guardian"}}
    )

    report = run_reconcile(temp_store, fix=False, codex_home=home)
    assert (report.codex_on_disk, report.codex_pending, report.codex_skipped_subagents) == (
        2,
        1,
        1,
    )
    assert report.codex_ingested == 0
    assert report.to_dict()["codex_pending"] == 1

    fixed = run_reconcile(temp_store, fix=True, codex_home=home)
    assert fixed.fix_applied
    assert fixed.codex_ingested == 1
    assert fixed.errors == []
    assert temp_store.sqlite.get_events(session_id="codex:primary")
    # Freshly written, so still settling: the exact pass stamps `archived`
    # and the finalizer waits for the thread to go quiet.
    assert temp_store.sqlite.analysis_stages()[str(home / "sessions/primary.jsonl")] == "archived"

    # Reconcile owned the lock throughout and released it afterwards.
    assert claim_ingest_lock(temp_store)
    release_ingest_lock(temp_store)

    after = run_reconcile(temp_store, fix=False, codex_home=home)
    assert (after.codex_pending, after.codex_ingested) == (0, 0)


def test_shared_mcp_and_codex_sync_are_registered_cli_commands():
    import inspect

    from typer.testing import CliRunner

    from longhand.cli import app

    commands = {
        (info.name or info.callback.__name__.replace("_", "-")): info
        for info in app.registered_commands
    }
    assert "shared-mcp" in commands
    assert "codex-sync" in commands

    runner = CliRunner()
    for name in ("shared-mcp", "codex-sync"):
        result = runner.invoke(app, [name, "--help"])
        assert result.exit_code == 0, result.output

    # Defaults and flags by introspection — rendered help wraps and hyphenates
    # long option names at narrow terminal widths, so it is not a stable oracle.
    signature = inspect.signature(commands["codex-sync"].callback)
    options = {name: param.default for name, param in signature.parameters.items()}
    assert options["limit"].default == 50
    assert options["max_file_kb"].default == 16384
    assert options["max_events"].default == 20000
    assert options["include_subagents"].default is False
    assert "--include-subagents" in options["include_subagents"].param_decls
    assert options["finalize_after"].default == 1800
    assert "--no-finalize" in options["no_finalize"].param_decls


def test_doctor_codex_capture_row(tmp_path, monkeypatch):
    from longhand.codex import CodexArchiveStore
    from longhand.setup_commands import _codex_capture_status

    store = CodexArchiveStore(tmp_path / "archive")
    # No Codex home at all: no row, so a machine without Codex never sees one.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "absent"))
    assert _codex_capture_status(store) is None

    home = tmp_path / "codex"
    write_rollout(home / "sessions/primary.jsonl", "primary")
    write_rollout(
        home / "sessions/guardian.jsonl", "guardian", source={"subagent": {"other": "guardian"}}
    )
    monkeypatch.setenv("CODEX_HOME", str(home))
    behind = _codex_capture_status(store)
    assert "1 of 1 rollouts new or changed" in behind
    assert "longhand codex-sync" in behind
    assert "1 subagent thread(s) skipped" in behind

    sync_codex(store, home)
    current = _codex_capture_status(store)
    assert "1/1 rollouts captured" in current
    assert "last capture" in current


def test_doctor_drift_row_names_the_codex_kind(tmp_path):
    from unittest.mock import MagicMock

    from longhand.setup_commands import _transcript_format_status
    from longhand.timeutil import utcnow
    from longhand.types import Event, EventType

    store = MagicMock()
    store.sqlite = SQLiteStore(tmp_path / "longhand.db")
    entry = {"type": "event_msg", "payload": {"type": "brand_new_kind"}}
    store.sqlite.insert_events(
        [
            Event(
                event_id="codex:drift:1",
                session_id="codex:drift",
                parent_event_id=None,
                event_type=EventType.UNKNOWN,
                sequence=1,
                timestamp=utcnow(),
                content=json.dumps(entry),
                raw=entry,
            )
        ]
    )
    row = _transcript_format_status(store)
    assert "event_msg/brand_new_kind ×1" in row


def test_scan_puts_recently_written_rollouts_in_settling(tmp_path):
    import os
    import time

    from longhand.codex import scan_codex_sessions

    source = tmp_path / "codex/sessions/rollout.jsonl"
    write_rollout(source)
    sqlite = SQLiteStore(tmp_path / "longhand.db")
    # Without an idle bound the scan is unchanged: a fresh rollout is a candidate.
    assert scan_codex_sessions(sqlite, tmp_path / "codex").candidates == [source]

    scan = scan_codex_sessions(sqlite, tmp_path / "codex", min_idle_seconds=600)
    assert (scan.candidates, scan.settling) == ([], [source])
    assert scan.on_disk == 1

    quiet = time.time() - 31 * 60
    os.utime(source, (quiet, quiet))
    scan = scan_codex_sessions(sqlite, tmp_path / "codex", min_idle_seconds=600)
    assert (scan.candidates, scan.settling) == ([source], [])


def _analyzing_store(sqlite, source):
    """A stand-in for LonghandStore: ingests into SQLite and stamps `analyzed`."""
    store = MagicMock()
    store.data_dir = source.parents[2] / "archive"
    store.sqlite = sqlite

    def ingest(session, events, run_analysis=True):
        sqlite.upsert_session(session)
        sqlite.insert_events(events)
        sqlite.log_ingestion(
            session.transcript_path, session.session_id, source.stat().st_size, len(events)
        )
        sqlite.set_analysis_stage(session.transcript_path, "analyzed")

    store.ingest_session.side_effect = ingest
    return store


def test_finalize_waits_for_quiet_then_runs_full_pipeline(tmp_path):
    import os
    import time

    from longhand.codex import CodexArchiveStore, finalize_codex

    source = tmp_path / "codex/sessions/rollout.jsonl"
    write_rollout(source)
    archive = CodexArchiveStore(tmp_path / "archive")
    assert sync_codex(archive, tmp_path / "codex")["ingested"] == 1
    assert archive.sqlite.analysis_stages()[str(source)] == "archived"

    # Still settling: nothing is finalized and the semantic store is never built.
    report = finalize_codex(
        archive.sqlite,
        tmp_path / "codex",
        lambda: pytest.fail("built the semantic store while the thread was settling"),
        idle_seconds=600,
    )
    assert (report["finalized"], report["settling"]) == (0, 1)
    assert archive.sqlite.analysis_stages()[str(source)] == "archived"

    quiet = time.time() - 31 * 60
    os.utime(source, (quiet, quiet))
    semantic = _analyzing_store(archive.sqlite, source)
    report = finalize_codex(archive.sqlite, tmp_path / "codex", lambda: semantic, idle_seconds=600)
    assert (report["finalized"], report["settling"], report["errors"]) == (1, 0, [])
    assert archive.sqlite.analysis_stages()[str(source)] == "analyzed"
    semantic.ingest_session.assert_called_once()

    # Nothing left to do: unchanged and analyzed.
    report = finalize_codex(archive.sqlite, tmp_path / "codex", lambda: semantic, idle_seconds=600)
    assert report["finalized"] == 0
    semantic.ingest_session.assert_called_once()


def test_finalize_after_append_regresses_then_recovers(tmp_path):
    import os
    import time

    from longhand.codex import CodexArchiveStore, finalize_codex

    source = tmp_path / "codex/sessions/rollout.jsonl"
    write_rollout(source)
    archive = CodexArchiveStore(tmp_path / "archive")
    semantic = _analyzing_store(archive.sqlite, source)
    quiet = time.time() - 31 * 60
    os.utime(source, (quiet, quiet))
    sync_codex(archive, tmp_path / "codex")
    assert finalize_codex(archive.sqlite, tmp_path / "codex", lambda: semantic)["finalized"] == 1

    # The thread resumes: live capture is exact-only again and the stage regresses.
    with source.open("a") as f:
        f.write(
            json.dumps(
                record(
                    "response_item",
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Anything else?"}],
                    },
                )
            )
            + "\n"
        )
    assert sync_codex(archive, tmp_path / "codex")["ingested"] == 1
    assert archive.sqlite.analysis_stages()[str(source)] == "archived"
    assert finalize_codex(archive.sqlite, tmp_path / "codex", lambda: semantic)["settling"] == 1

    # Ten quiet minutes later it is finalized once more — one re-embed per resume.
    os.utime(source, (quiet, quiet))
    assert finalize_codex(archive.sqlite, tmp_path / "codex", lambda: semantic)["finalized"] == 1
    assert archive.sqlite.analysis_stages()[str(source)] == "analyzed"
    assert len(archive.sqlite.get_events(session_id="codex:test-id")) == 11


def test_reconcile_finalizes_quiet_codex_rollouts(temp_store, tmp_path, monkeypatch):
    import os
    import time

    from longhand.recall import reconcile as reconcile_mod
    from longhand.recall.reconcile import run_reconcile

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [])
    home = tmp_path / "codex"
    quiet_path = home / "sessions/quiet.jsonl"
    active_path = home / "sessions/active.jsonl"
    write_rollout(quiet_path, "quiet")
    write_rollout(active_path, "active")
    quiet = time.time() - 31 * 60
    os.utime(quiet_path, (quiet, quiet))

    dry = run_reconcile(temp_store, fix=False, codex_home=home)
    assert (dry.codex_pending, dry.codex_unfinalized, dry.codex_settling) == (2, 1, 1)
    assert dry.to_dict()["codex_unfinalized"] == 1
    assert dry.codex_finalized == 0

    fixed = run_reconcile(temp_store, fix=True, codex_home=home)
    assert fixed.errors == []
    assert (fixed.codex_ingested, fixed.codex_finalized, fixed.codex_settling) == (2, 1, 1)
    stages = temp_store.sqlite.analysis_stages()
    assert stages[str(quiet_path)] == "analyzed"
    assert stages[str(active_path)] == "archived"
    # The quiet thread is now recallable: its events reached the vector store.
    assert temp_store.vectors.events_collection.get(
        where={"session_id": "codex:quiet"}, include=[]
    )["ids"]

    after = run_reconcile(temp_store, fix=False, codex_home=home)
    assert (after.codex_pending, after.codex_unfinalized, after.codex_settling) == (0, 0, 1)


def _cli_json(output):
    """Rich wraps long lines at the runner's width; rejoin before parsing."""
    return json.loads("".join(output.splitlines()))


def test_codex_sync_cli_never_loads_model_while_threads_settle(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from longhand.cli import app

    home = tmp_path / "codex"
    write_rollout(home / "sessions/active.jsonl", "active")
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(
        "longhand.storage.store.VectorStore", lambda *a, **k: pytest.fail("loaded vectors")
    )
    result = CliRunner().invoke(app, ["codex-sync"])
    assert result.exit_code == 0, result.output
    report = _cli_json(result.output)
    assert (report["ingested"], report["finalized"], report["settling"]) == (1, 0, 1)


def test_codex_sync_cli_finalizes_quiet_threads(tmp_path, monkeypatch):
    import os
    import time

    from typer.testing import CliRunner

    from longhand.cli import _commands, app

    home = tmp_path / "codex"
    source = home / "sessions/quiet.jsonl"
    write_rollout(source, "quiet")
    quiet = time.time() - 31 * 60
    os.utime(source, (quiet, quiet))
    archive = tmp_path / "archive"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(archive))
    archive.mkdir()
    sqlite = SQLiteStore(archive / "longhand.db")
    semantic = _analyzing_store(sqlite, source)
    semantic.data_dir = archive
    monkeypatch.setattr(_commands, "_get_store", lambda data_dir=None: semantic)

    result = CliRunner().invoke(app, ["codex-sync"])
    assert result.exit_code == 0, result.output
    report = _cli_json(result.output)
    assert (report["ingested"], report["finalized"], report["settling"]) == (1, 1, 0)
    assert sqlite.analysis_stages()[str(source)] == "analyzed"

    # Opting out leaves the quiet thread archived: nothing else changes.
    with sqlite.connect() as conn:
        conn.execute("UPDATE ingestion_log SET analysis_stage = 'archived'")
    semantic.ingest_session.reset_mock()
    result = CliRunner().invoke(app, ["codex-sync", "--no-finalize"])
    assert result.exit_code == 0, result.output
    assert "finalized" not in _cli_json(result.output)
    semantic.ingest_session.assert_not_called()


def test_doctor_codex_finalizer_row(tmp_path, monkeypatch):
    import os
    import time

    from longhand.codex import CodexArchiveStore
    from longhand.setup_commands import _codex_finalize_status

    store = CodexArchiveStore(tmp_path / "archive")
    home = tmp_path / "codex"
    source = home / "sessions/active.jsonl"
    write_rollout(source, "active")
    monkeypatch.setenv("CODEX_HOME", str(home))
    # Nothing captured exact-only yet: no row.
    assert _codex_finalize_status(store) is None

    sync_codex(store, home)
    settling = _codex_finalize_status(store)
    assert "1 Codex thread(s) active" in settling
    assert "30 min" in settling

    quiet = time.time() - 31 * 60
    os.utime(source, (quiet, quiet))
    waiting = _codex_finalize_status(store)
    assert "1 awaiting the finalizer" in waiting
    assert "longhand codex-sync" in waiting

    store.sqlite.set_analysis_stage(str(source), "analyzed")
    assert _codex_finalize_status(store) is None


def test_reconcile_cli_reports_codex_finalization(temp_store, tmp_path, monkeypatch):
    import os
    import time

    from typer.testing import CliRunner

    from longhand.cli import app
    from longhand.codex import CodexArchiveStore
    from longhand.recall import reconcile as reconcile_mod

    monkeypatch.setattr(reconcile_mod, "discover_sessions", lambda: [])
    home = tmp_path / "codex"
    source = home / "sessions/quiet.jsonl"
    write_rollout(source, "quiet")
    quiet = time.time() - 31 * 60
    os.utime(source, (quiet, quiet))
    monkeypatch.setenv("CODEX_HOME", str(home))
    sync_codex(CodexArchiveStore(temp_store.data_dir, sqlite=temp_store.sqlite), home)

    runner = CliRunner()
    dry = runner.invoke(app, ["reconcile", "--data-dir", str(temp_store.data_dir)])
    assert dry.exit_code == 0, dry.output
    assert "1 quiet thread(s) awaiting the full pipeline" in dry.output

    fixed = runner.invoke(app, ["reconcile", "--fix", "--data-dir", str(temp_store.data_dir)])
    assert fixed.exit_code == 0, fixed.output
    assert "Finalized 1 Codex thread(s)" in fixed.output
    assert temp_store.sqlite.analysis_stages()[str(source)] == "analyzed"
