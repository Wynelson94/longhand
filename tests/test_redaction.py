"""Tests for opt-in secret redaction.

All fixture values are obviously fake, constructed from repeated filler
characters — never realistic keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand.cli import app
from longhand.parser import JSONLParser
from longhand.redaction import (
    _redact_obj,
    redact_event,
    redact_text,
    redaction_enabled,
    scan_text,
)
from longhand.storage.store import LonghandStore

FAKE_ANTHROPIC = "sk-ant-" + "a0" * 25  # 50-char body
FAKE_OPENAI = "sk-" + "abc123" * 4
FAKE_GITHUB = "ghp_" + "A1" * 16
FAKE_AWS = "AKIA" + "EXAMPLEKEY123456"
FAKE_GOOGLE = "AIza" + "B" * 35
FAKE_SLACK = "xoxb-" + "123456789012"
FAKE_STRIPE = "sk_test_" + "a1" * 12
FAKE_JWT = "eyJ" + "a" * 12 + "." + "eyJ" + "b" * 12 + "." + "c" * 12
FAKE_DB_URL = "postgres://user:hunter2@db.example.com/prod"
FAKE_SSH_HEADER = "-----BEGIN OPENSSH PRIVATE KEY-----"
FAKE_SSN = "123-45-6789"
FAKE_CARD_REAL = "4012-8888-8888-1881"  # Luhn-valid, varied digits
FAKE_CARD_TEST = "4111111111111111"  # Luhn-valid but only 2 distinct digits
FAKE_CARD_JUNK = "1111 1111 1111 1111"  # fails BIN + variety


@pytest.mark.parametrize(
    "name,value",
    [
        ("anthropic_key", FAKE_ANTHROPIC),
        ("openai_key", FAKE_OPENAI),
        ("github_token", FAKE_GITHUB),
        ("aws_access_key_id", FAKE_AWS),
        ("google_api_key", FAKE_GOOGLE),
        ("slack_token", FAKE_SLACK),
        ("stripe_key", FAKE_STRIPE),
        ("jwt_token", FAKE_JWT),
        ("db_url_with_password", FAKE_DB_URL),
        ("ssh_private_key", FAKE_SSH_HEADER),
        ("ssn", FAKE_SSN),
        ("credit_card", FAKE_CARD_REAL),
    ],
)
def test_redact_text_masks_each_pattern(name: str, value: str):
    text = f"setting the credential to {value} in the env"
    redacted, count = redact_text(text)
    assert count >= 1
    assert value not in redacted
    assert scan_text(text).get(name, 0) >= 1


def test_mask_keeps_only_edges_and_length():
    redacted, count = redact_text(FAKE_ANTHROPIC)
    assert count == 1
    assert redacted.startswith(FAKE_ANTHROPIC[:4])
    assert redacted.endswith(f"(len={len(FAKE_ANTHROPIC)})")
    # The middle of the secret must be gone
    assert FAKE_ANTHROPIC[8:-8] not in redacted


def test_implausible_cards_left_alone():
    for value in (FAKE_CARD_TEST, FAKE_CARD_JUNK):
        redacted, count = redact_text(f"test card {value} ok")
        assert count == 0
        assert value in redacted


def test_clean_text_passthrough():
    text = "nothing secret here, just a normal sentence with sk- prefix talk"
    redacted, count = redact_text(text)
    assert count == 0
    assert redacted == text


def test_scan_text_never_mutates_and_counts():
    text = f"{FAKE_ANTHROPIC} and {FAKE_GITHUB}"
    counts = scan_text(text)
    assert counts["anthropic_key"] == 1
    assert counts["github_token"] == 1


def test_redact_obj_walks_nested_structures():
    obj = {
        "command": f"export KEY={FAKE_ANTHROPIC}",
        "nested": {"list": [FAKE_GITHUB, "clean"], "n": 42},
    }
    redacted, count = _redact_obj(obj)
    flat = json.dumps(redacted)
    assert count == 2
    assert FAKE_ANTHROPIC not in flat
    assert FAKE_GITHUB not in flat
    assert redacted["nested"]["n"] == 42
    assert "clean" in flat


def _write_transcript(path: Path, secret: str) -> None:
    entries = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "redact-test",
            "timestamp": "2026-06-01T10:00:00.000Z",
            "cwd": "/tmp/x",
            "message": {"role": "user", "content": f"my key is {secret} please use it"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "redact-test",
            "timestamp": "2026-06-01T10:00:01.000Z",
            "cwd": "/tmp/x",
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [{"type": "text", "text": "Using the key you provided."}],
            },
        },
    ]
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _enable_redaction(home: Path) -> None:
    cfg_dir = home / ".longhand"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({"redact": {"enabled": True}}))


def test_redaction_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert redaction_enabled() is False

    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, FAKE_ANTHROPIC)
    events = list(JSONLParser(transcript).parse_events())
    assert any(FAKE_ANTHROPIC in e.content for e in events)


def test_parser_redacts_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _enable_redaction(tmp_path)
    assert redaction_enabled() is True

    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, FAKE_ANTHROPIC)
    events = list(JSONLParser(transcript).parse_events())

    joined_content = " ".join(e.content for e in events)
    joined_raw = json.dumps([e.raw for e in events])
    assert FAKE_ANTHROPIC not in joined_content
    assert FAKE_ANTHROPIC not in joined_raw
    assert "(len=" in joined_content  # mask is present where the key was


def test_tail_parse_redacts_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _enable_redaction(tmp_path)

    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, FAKE_ANTHROPIC)
    events, _ = JSONLParser(transcript).parse_tail_from_offset(0)
    assert events
    joined = " ".join(e.content for e in events) + json.dumps([e.raw for e in events])
    assert FAKE_ANTHROPIC not in joined


def test_redact_event_covers_tool_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, "no secret")
    event = next(iter(JSONLParser(transcript).parse_events()))

    event.content = f"ran with {FAKE_ANTHROPIC}"
    event.tool_output = f"printed {FAKE_GITHUB}"
    event.tool_input = {"command": f"echo {FAKE_AWS}"}
    event.raw = {"message": {"content": FAKE_ANTHROPIC}}

    n = redact_event(event)
    assert n == 4
    dump = event.content + (event.tool_output or "") + json.dumps(event.tool_input)
    dump += json.dumps(event.raw)
    assert FAKE_ANTHROPIC not in dump
    assert FAKE_GITHUB not in dump
    assert FAKE_AWS not in dump


def test_cli_redact_scan_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """End-to-end: ingest with redaction OFF, scan finds the key (without
    printing it), apply masks it in SQLite and the vector index."""
    monkeypatch.setenv("HOME", str(tmp_path))

    transcript = tmp_path / "session.jsonl"
    _write_transcript(transcript, FAKE_ANTHROPIC)
    parser = JSONLParser(transcript)
    events = list(parser.parse_events())
    session = parser.build_session(events)

    data_dir = tmp_path / "store"
    store = LonghandStore(data_dir=data_dir)
    store.ingest_session(session, events)

    runner = CliRunner()

    # Scan: reports the pattern name, never the value
    result = runner.invoke(app, ["redact", "--data-dir", str(data_dir)])
    assert result.exit_code == 0
    assert "anthropic_key" in result.stdout
    assert FAKE_ANTHROPIC not in result.stdout

    # Apply
    result = runner.invoke(app, ["redact", "--apply", "--yes", "--data-dir", str(data_dir)])
    assert result.exit_code == 0
    assert FAKE_ANTHROPIC not in result.stdout

    # SQLite is clean — check every text column we claim to cover
    fresh = LonghandStore(data_dir=data_dir)
    with fresh.sqlite.connect() as conn:
        rows = conn.execute("SELECT content, tool_output, raw_json FROM events").fetchall()
    blob = json.dumps([dict(r) for r in rows])
    assert FAKE_ANTHROPIC not in blob

    # Vector documents are clean
    docs = fresh.vectors.events_collection.get(include=["documents"]).get("documents") or []
    assert all(FAKE_ANTHROPIC not in (d or "") for d in docs)

    # Re-running the scan reports a clean store
    result = runner.invoke(app, ["redact", "--data-dir", str(data_dir)])
    assert result.exit_code == 0
    assert "clean" in result.stdout.lower()


def test_retroactive_redact_covers_segments_and_git_commits(tmp_path: Path):
    """`redact --apply` must reach conversation_segments and git_operations.

    Regression: the table map said "segments" — a nonexistent table — so the
    per-table except skipped ALL segment text (topic/summary/keywords)
    silently, and git commit messages were never listed at all. topic holds
    the segment's verbatim first user message.
    """
    data_dir = tmp_path / "lh"
    store = LonghandStore(data_dir=data_dir)
    store.sqlite.insert_segments(
        [
            {
                "segment_id": "seg-1",
                "session_id": "s1",
                "started_at": "2026-07-01T10:00:00Z",
                "ended_at": "2026-07-01T10:10:00Z",
                "start_sequence": 1,
                "end_sequence": 5,
                "topic": f"set the key to {FAKE_ANTHROPIC} please",
                "summary": "credentials discussion",
            }
        ]
    )
    store.sqlite.insert_git_operations(
        [
            {
                "git_op_id": "g1",
                "session_id": "s1",
                "event_id": "e1",
                "operation_type": "commit",
                "commit_message": f"chore: rotate {FAKE_ANTHROPIC}",
                "timestamp": "2026-07-01T10:05:00Z",
            }
        ]
    )

    local_runner = CliRunner()
    result = local_runner.invoke(app, ["redact", "--apply", "--yes", "--data-dir", str(data_dir)])
    assert result.exit_code == 0

    fresh = LonghandStore(data_dir=data_dir)
    with fresh.sqlite.connect() as conn:
        topic = conn.execute(
            "SELECT topic FROM conversation_segments WHERE segment_id = 'seg-1'"
        ).fetchone()[0]
        msg = conn.execute(
            "SELECT commit_message FROM git_operations WHERE git_op_id = 'g1'"
        ).fetchone()[0]
    assert FAKE_ANTHROPIC not in topic
    assert FAKE_ANTHROPIC not in msg
