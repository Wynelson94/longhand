"""Canary: a session that launches in $HOME and `cd`s into a real project
mid-session must be attributed to the project, not to NULL or to $HOME.

Bug class: project inference that uses only the first event's cwd loses
attribution for the common pattern of starting a Claude Code session from
the user's home dir and then `cd`-ing into the actual project. The session
gets project_id = NULL, never appears in `recall_project_status(project)`,
and the user sees "No session history found" despite the transcript
existing on disk.

Real-world instance (2026-04-22): Nate's `recall_project_status("bsoi-mesh-kit")`
returned "No session history found" despite four JSONL transcripts and a
2,526-line work session being on disk. Caught by dogfooding, fixed in v0.6.0
via `_pick_best_project_cwd` which tallies all event cwds, filters $HOME and
non-marker dirs, and picks the mode.
"""

from __future__ import annotations

from pathlib import Path

from tests.canary_harness import ProjectStatusAssertion

DESCRIPTION = (
    "A session whose first event has cwd=$HOME but later events `cd` into a "
    "real project must be attributed to that project, not to home / NULL."
)


def _ts(minute: int) -> str:
    return f"2026-04-22T15:{minute:02d}:00.000Z"


SESSION_ID = "canary-home-cd-session"


def setup(tmp_path: Path) -> None:
    """Create a real project root with a `.git` marker so cwd resolution succeeds.

    project_inference walks up from each event's cwd looking for a marker
    (.git, pyproject.toml, etc) on disk. Without an actual marker file the
    resolver returns None and the cwd is filtered out, masking the bug.
    """
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(exist_ok=True)
    project = fake_home / "Projects" / "myproject"
    project.mkdir(parents=True, exist_ok=True)
    (project / ".git").mkdir(exist_ok=True)


def build_sessions(tmp_path: Path) -> list[tuple[str, list[dict]]]:
    """Build a session whose first event cwd is $HOME and later events are
    inside the project. The mode of all-event cwds is the project; if
    inference uses only first_real_cwd, attribution lands on $HOME.
    """
    fake_home = str(tmp_path / "fake_home")
    project_dir = str(tmp_path / "fake_home" / "Projects" / "myproject")

    events = [
        # 1. Session starts in $HOME — user types `claude`, opens a fresh session.
        {
            "type": "user",
            "uuid": "u-1",
            "parentUuid": None,
            "sessionId": SESSION_ID,
            "timestamp": _ts(0),
            "cwd": fake_home,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": "let me check on myproject — cd into it",
            },
        },
        # 2-7. All subsequent events happen inside the project after the cd.
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
                    {"type": "text", "text": "Reading myproject's README."},
                    {
                        "type": "tool_use",
                        "id": "t-1",
                        "name": "Read",
                        "input": {"file_path": f"{project_dir}/README.md"},
                    },
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
                    {"type": "tool_result", "tool_use_id": "t-1", "content": "# myproject\n"}
                ],
            },
            "toolUseResult": {"success": True},
        },
        {
            "type": "user",
            "uuid": "u-2",
            "parentUuid": "r-1",
            "sessionId": SESSION_ID,
            "timestamp": _ts(3),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {"role": "user", "content": "fix the typo in the heading"},
        },
        {
            "type": "assistant",
            "uuid": "a-2",
            "parentUuid": "u-2",
            "sessionId": SESSION_ID,
            "timestamp": _ts(4),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "model": "claude-sonnet-4-6",
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t-2",
                        "name": "Edit",
                        "input": {
                            "file_path": f"{project_dir}/README.md",
                            "old_string": "# myproject\n",
                            "new_string": "# my project\n",
                            "replace_all": False,
                        },
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "r-2",
            "parentUuid": "a-2",
            "sessionId": SESSION_ID,
            "timestamp": _ts(5),
            "cwd": project_dir,
            "isSidechain": False,
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t-2", "content": "Updated."}
                ],
            },
            "toolUseResult": {"success": True},
        },
    ]
    return [("home_cd_session.jsonl", events)]


ASSERTIONS = [
    # If inference uses first-event cwd only, this fails — the project is
    # never linked because $HOME was the first cwd. With v0.6.0's mode-of-cwds
    # logic, the project IS linked and the assertion passes.
    ProjectStatusAssertion(
        project_query="myproject",
        min_indexed_sessions=1,
        description=(
            "Session that starts in $HOME and cd's into 'myproject' must be "
            "attributed to myproject (the dominant cwd), not to NULL"
        ),
    ),
]
