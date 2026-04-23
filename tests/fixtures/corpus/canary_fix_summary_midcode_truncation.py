"""Canary: fix_summary diff content must not end mid-token.

Bug class: `_compose_fix_summary` slices old_content/new_content with `[:120]`
— a hard byte-count cut that lands wherever the budget falls. Real samples
in the reference corpus end with things like `family?:'` (mid-attribute),
`strin'` (mid-keyword "string"), and `givenName?: strin' → 'interface...`
(mid-type-annotation). The user reads recall narratives where every other
diff trails off mid-line in an unhelpful place.

Fix: truncate at the last whitespace inside the budget and append `…` so
truncation is visible. Falls back to a hard cut + ellipsis only when no
whitespace exists in the budget window (rare).
"""

from __future__ import annotations

from pathlib import Path

from tests.canary_harness import OutputAssertion

DESCRIPTION = (
    "When fix_summary diff content is truncated, it must end at a clean "
    "boundary (whitespace + ellipsis), not mid-word/mid-token."
)


def _ts(minute: int) -> str:
    return f"2026-04-15T13:{minute:02d}:00.000Z"


SESSION_ID = "canary-midcode-trunc-session"

# A long edit body with internal structure — when truncated at byte 120, the
# naive slice will land mid-keyword. Picked specifically so a clean cut
# would land at a space rather than mid-token.
_OLD = (
    "interface PhoneContact {\n"
    "  contactId: string\n"
    "  name?: { given?: string; family?: string }\n"
    "  emails?: string[]\n"
    "  phones?: string[]\n"
    "}\n"
)
_NEW = (
    "interface PhoneContact {\n"
    "  id?: string\n"
    "  givenName?: string\n"
    "  familyName?: string\n"
    "  emailAddresses?: string[]\n"
    "  phoneNumbers?: string[]\n"
    "}\n"
)


def setup(tmp_path: Path) -> None:
    project = tmp_path / "fakeproj"
    project.mkdir(exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)


def build_sessions(tmp_path: Path) -> list[tuple[str, list[dict]]]:
    project_dir = str(tmp_path / "fakeproj")
    file_path = f"{project_dir}/types.ts"
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
                "content": "rename the contact fields to match the platform shape",
            },
        },
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
                    {"type": "text", "text": "Reading the current types."},
                    {"type": "tool_use", "id": "t-r-1", "name": "Read",
                     "input": {"file_path": file_path}},
                ],
            },
        },
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
                    {"type": "tool_result", "tool_use_id": "t-r-1", "content": _OLD},
                ],
            },
            "toolUseResult": {"success": True},
        },
        # Failing build to anchor the episode.
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
                    {"type": "tool_use", "id": "t-b-1", "name": "Bash",
                     "input": {"command": "tsc --noEmit types.ts"}},
                ],
            },
        },
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
                    {"type": "tool_result", "tool_use_id": "t-b-1",
                     "content": (
                         f"Error compiling {file_path}\n"
                         "  Cannot find name 'string'\n"
                         "  at line 3, column 28\n"
                     )},
                ],
            },
            "toolUseResult": {"success": False},
        },
        # Intent text + Edit with the long old/new — this is the row whose
        # fix_summary will get truncated.
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
                        "type": "text",
                        "text": "Renaming the fields to match the platform contact schema.",
                    },
                    {
                        "type": "tool_use",
                        "id": "t-e-1",
                        "name": "Edit",
                        "input": {
                            "file_path": file_path,
                            "old_string": _OLD,
                            "new_string": _NEW,
                            "replace_all": False,
                        },
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
                    {"type": "tool_result", "tool_use_id": "t-e-1", "content": "Updated."},
                ],
            },
            "toolUseResult": {"success": True},
        },
    ]
    return [("midcode_session.jsonl", events)]


def _no_midword_truncation(store):
    """Predicate: truncated fix_summary content must end at a clean boundary.

    The diff format is `<Tool> on <file>: '<old>' → '<new>'`. When old/new
    are truncated, they should end with whitespace + `…` (ellipsis), not
    mid-word/mid-token. The bug is the naive `[:120]` slice.

    We detect mid-token cuts by checking whether the new-content slice ends
    with a word character followed immediately by the closing quote — the
    classic shape of an arbitrary mid-keyword cut.
    """
    import re

    with store.sqlite.connect() as conn:
        rows = list(
            conn.execute(
                "SELECT episode_id, fix_summary FROM episodes "
                "WHERE fix_summary IS NOT NULL AND fix_summary LIKE '%→%'"
            )
        )

    # Match the new-content slice — the part after `→ '` and before the
    # final closing `'`.
    new_pattern = re.compile(r"→ '(.+?)'(?:\.|$)", re.DOTALL)
    bad: list[tuple[str, str]] = []
    for r in rows:
        fs = r["fix_summary"] or ""
        match = new_pattern.search(fs)
        if not match:
            continue
        new_slice = match.group(1)
        # Truncated slices should end with "…" — anything else with length
        # > some-threshold is a candidate for mid-token cut.
        if len(new_slice) >= 100 and not new_slice.endswith("…"):
            # Mid-word signal: ends with a word char (suggests cut mid-token)
            if re.search(r"[A-Za-z0-9_]$", new_slice):
                bad.append((r["episode_id"], new_slice[-40:]))

    if not bad:
        return True, ""
    samples = "\n".join(f"  {eid}: ...{tail!r}" for eid, tail in bad[:3])
    return False, (
        f"{len(bad)} of {len(rows)} fix_summaries end mid-token in the "
        f"new-content slice (no whitespace + ellipsis). Samples:\n{samples}"
    )


ASSERTIONS = [
    OutputAssertion(
        description=(
            "Truncated fix_summary diff content must end at whitespace + "
            "ellipsis, not mid-word"
        ),
        predicate=_no_midword_truncation,
    ),
]
