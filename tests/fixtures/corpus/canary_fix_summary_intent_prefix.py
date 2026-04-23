"""Canary: extracted episode fix_summary must not leak the "Intent:" label.

Bug class: `_compose_fix_summary` prepends a literal "Intent: " label to the
assistant's intent text "so the embedding treats it structurally" (per the
original comment). The label leaks into the user-visible narrative — every
recall narrative for an affected episode opens with `### The fix\nIntent:
<rest of text>` instead of clean prose.

Real-world prevalence (2026-04-23 audit of /Users/natenelson/.longhand):
100 of 204 episodes (49%) have fix_summary that starts with "Intent:". This
is the dominant rough-edge in narrative output for users with a real corpus.

Fix scope: drop the "Intent: " label. The intent_text itself is already
prose Claude wrote — it doesn't need a structural marker. The embedding
sees the text either way.
"""

from __future__ import annotations

from pathlib import Path

from tests.canary_harness import OutputAssertion

DESCRIPTION = (
    "Extracted fix_summary must not start with the literal 'Intent:' label. "
    "Half the existing real corpus has this bug — every affected episode "
    "renders ugly text in recall narratives."
)


def _ts(minute: int) -> str:
    return f"2026-04-15T12:{minute:02d}:00.000Z"


SESSION_ID = "canary-intent-prefix-session"


def setup(tmp_path: Path) -> None:
    """Project marker so cwd resolution succeeds."""
    project = tmp_path / "fakeproj"
    project.mkdir(exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)


def build_sessions(tmp_path: Path) -> list[tuple[str, list[dict]]]:
    """Build a session that produces an extracted episode.

    Episode extraction requires an error-detected tool_result to anchor the
    arc: user ask → failing tool → assistant intent + fix → success → verify.
    The substantive assistant_text BEFORE the fix is what gets pulled into
    fix_summary as the "intent" portion (line 410 of episode_extraction.py).
    """
    project_dir = str(tmp_path / "fakeproj")
    file_path = f"{project_dir}/calc.py"
    events = [
        {
            "type": "user",
            "uuid": "u-1",
            "parentUuid": None,
            "sessionId": SESSION_ID,
            "timestamp": _ts(0),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": "calc.py is throwing on negative inputs, can you fix it?",
            },
        },
        # 1. Run the failing test — produces the error tool_result that
        # anchors the episode.
        {
            "type": "assistant",
            "uuid": "a-1",
            "parentUuid": "u-1",
            "sessionId": SESSION_ID,
            "timestamp": _ts(1),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me reproduce the failure."},
                    {
                        "type": "tool_use",
                        "id": "t-bash-1",
                        "name": "Bash",
                        "input": {"command": "python calc.py -5"},
                    },
                ],
            },
        },
        # 2. Failing tool_result — error_detected on this triggers extraction.
        {
            "type": "user",
            "uuid": "r-1",
            "parentUuid": "a-1",
            "sessionId": SESSION_ID,
            "timestamp": _ts(2),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-bash-1",
                        "content": (
                            "Traceback (most recent call last):\n"
                            '  File "calc.py", line 8, in main\n'
                            "    result = math.sqrt(n)\n"
                            "ValueError: math domain error\n"
                        ),
                    }
                ],
            },
            "toolUseResult": {"success": False},
        },
        # 3. Substantive assistant_text — this becomes the "intent" pulled
        # into fix_summary. Plain prose, no "Intent:" wrapper anywhere in it.
        {
            "type": "assistant",
            "uuid": "a-2",
            "parentUuid": "r-1",
            "sessionId": SESSION_ID,
            "timestamp": _ts(3),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": (
                            "math.sqrt blows up on negatives. The fix is to "
                            "guard the input — return None or raise a friendlier "
                            "error before calling sqrt on a negative."
                        ),
                    },
                    {
                        "type": "text",
                        "text": (
                            "I'll guard against negative inputs in calc.py — "
                            "they currently slip through to math.sqrt and "
                            "raise an unhelpful domain error. Adding an "
                            "explicit check that returns None for negatives."
                        ),
                    },
                    {
                        "type": "tool_use",
                        "id": "t-edit-1",
                        "name": "Edit",
                        "input": {
                            "file_path": file_path,
                            "old_string": "result = math.sqrt(n)",
                            "new_string": "if n < 0:\n        return None\n    result = math.sqrt(n)",
                            "replace_all": False,
                        },
                    },
                ],
            },
        },
        # 4. Edit success.
        {
            "type": "user",
            "uuid": "r-2",
            "parentUuid": "a-2",
            "sessionId": SESSION_ID,
            "timestamp": _ts(4),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-edit-1",
                        "content": f"The file {file_path} has been updated successfully.",
                    }
                ],
            },
            "toolUseResult": {"success": True},
        },
        # 5. Verification — rerun the command, this time it succeeds.
        {
            "type": "assistant",
            "uuid": "a-3",
            "parentUuid": "r-2",
            "sessionId": SESSION_ID,
            "timestamp": _ts(5),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t-bash-2",
                        "name": "Bash",
                        "input": {"command": "python calc.py -5"},
                    },
                ],
            },
        },
        {
            "type": "user",
            "uuid": "r-3",
            "parentUuid": "a-3",
            "sessionId": SESSION_ID,
            "timestamp": _ts(6),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t-bash-2",
                        "content": "result: None\n",
                    }
                ],
            },
            "toolUseResult": {"success": True},
        },
    ]
    return [("intent_prefix_session.jsonl", events)]


def _no_intent_label_in_fix_summary(store):
    """Predicate: no episode in the store has fix_summary starting with 'Intent:'."""
    with store.sqlite.connect() as conn:
        rows = list(
            conn.execute(
                "SELECT episode_id, substr(fix_summary, 1, 200) as preview "
                "FROM episodes WHERE fix_summary IS NOT NULL AND fix_summary != ''"
            )
        )
    bad = [r for r in rows if (r["preview"] or "").startswith("Intent:")]
    if not bad:
        return True, ""
    samples = "\n".join(f"  {r['episode_id']}: {r['preview'][:120]}..." for r in bad[:3])
    return False, (
        f"{len(bad)} of {len(rows)} fix_summaries leak the 'Intent:' label. "
        f"Samples:\n{samples}"
    )


ASSERTIONS = [
    OutputAssertion(
        description=(
            "Extracted fix_summary must not start with literal 'Intent:' label "
            "(would render as ugly prefix in every recall narrative)"
        ),
        predicate=_no_intent_label_in_fix_summary,
    ),
]
