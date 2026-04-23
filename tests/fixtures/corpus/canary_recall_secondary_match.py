"""Canary: recall must surface secondary-session matches when a focused
on-topic session wins the primary slot.

Bug class: a focused, dense conversation about topic X outranks a
mixed-topic session that briefly mentions X. Without surfacing the secondary
hit, recall goes silent on the second session even though it contains the
distinctive term the user asked about.

Real-world instance (2026-04-23): session `eef364a9` contained "things v0.8.0
should fix" inside a v0.7.0 release-notes draft. A focused v0.5.x roadmap
session (`015252dd`) outranked it for the query "longhand v0.8 future plans"
and `eef364a9` was silently dropped.

This canary distills the bug to two minimal sessions: a focused
"color-picker debugging" session and a mixed-topic "v2 release notes" session
that briefly mentions color-picker accessibility followups for v3.
"""

from __future__ import annotations

from tests.canary_harness import RecallAssertion

DESCRIPTION = (
    "Focused on-topic session outranks a mixed-topic session that briefly "
    "mentions the queried term. The mixed-topic session must still surface "
    "(in episodes, segments, or the narrative footer)."
)


def _ts(minute: int) -> str:
    return f"2026-04-15T10:{minute:02d}:00.000Z"


# ─── Session A: focused color-picker debugging ──────────────────────────────
#
# This session is densely about color picker bugs. Will rank highest for the
# query "color picker accessibility plans" because the topic is the entire
# session.

SESSION_A_ID = "canary-focused-cpicker"
SESSION_A_EVENTS = [
    {
        "type": "user",
        "uuid": "a-user-1",
        "parentUuid": None,
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(0),
        "cwd": "/tmp/canary-app",
        "gitBranch": "main",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": (
                "The color picker component is broken — keyboard users can't "
                "navigate between swatches and screen readers don't announce "
                "the active color. Can you investigate and fix the accessibility?"
            ),
        },
    },
    {
        "type": "assistant",
        "uuid": "a-asst-1",
        "parentUuid": "a-user-1",
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(1),
        "cwd": "/tmp/canary-app",
        "isSidechain": False,
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": (
                        "The color picker is missing keyboard handling and ARIA "
                        "attributes. I need to add arrow-key navigation between "
                        "swatches and aria-live for the active color announcement."
                    ),
                },
                {
                    "type": "text",
                    "text": "Let me read the color picker component first.",
                },
                {
                    "type": "tool_use",
                    "id": "a-tool-1",
                    "name": "Read",
                    "input": {"file_path": "/tmp/canary-app/ColorPicker.tsx"},
                },
            ],
        },
    },
    {
        "type": "user",
        "uuid": "a-result-1",
        "parentUuid": "a-asst-1",
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(2),
        "cwd": "/tmp/canary-app",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "a-tool-1",
                    "content": (
                        "export function ColorPicker({swatches, onChange}) {\n"
                        "  return <div>{swatches.map(s => <button onClick={() => onChange(s)}>{s}</button>)}</div>;\n"
                        "}\n"
                    ),
                }
            ],
        },
        "toolUseResult": {"success": True},
    },
    {
        "type": "assistant",
        "uuid": "a-asst-2",
        "parentUuid": "a-result-1",
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(3),
        "cwd": "/tmp/canary-app",
        "isSidechain": False,
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": (
                        "No keyboard handlers, no ARIA. I'll add roving tabindex "
                        "for arrow-key navigation between swatches and an "
                        "aria-live region for the active color."
                    ),
                },
                {
                    "type": "tool_use",
                    "id": "a-tool-2",
                    "name": "Edit",
                    "input": {
                        "file_path": "/tmp/canary-app/ColorPicker.tsx",
                        "old_string": (
                            "export function ColorPicker({swatches, onChange}) {\n"
                            "  return <div>{swatches.map(s => <button onClick={() => onChange(s)}>{s}</button>)}</div>;\n"
                            "}\n"
                        ),
                        "new_string": (
                            "export function ColorPicker({swatches, onChange}) {\n"
                            "  const [active, setActive] = useState(0);\n"
                            "  const onKey = (e) => { if (e.key === 'ArrowRight') setActive(i => (i+1) % swatches.length); };\n"
                            "  return (\n"
                            "    <div role='listbox' aria-label='Color picker' onKeyDown={onKey}>\n"
                            "      {swatches.map((s, i) => (\n"
                            "        <button role='option' aria-selected={i===active} tabIndex={i===active?0:-1} onClick={() => onChange(s)}>{s}</button>\n"
                            "      ))}\n"
                            "      <span aria-live='polite'>Active color: {swatches[active]}</span>\n"
                            "    </div>\n"
                            "  );\n"
                            "}\n"
                        ),
                        "replace_all": False,
                    },
                },
            ],
        },
    },
    {
        "type": "user",
        "uuid": "a-result-2",
        "parentUuid": "a-asst-2",
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(4),
        "cwd": "/tmp/canary-app",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "a-tool-2",
                    "content": "The file has been updated successfully.",
                }
            ],
        },
        "toolUseResult": {"success": True},
    },
    {
        "type": "assistant",
        "uuid": "a-asst-3",
        "parentUuid": "a-result-2",
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(5),
        "cwd": "/tmp/canary-app",
        "isSidechain": False,
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "a-tool-3",
                    "name": "Bash",
                    "input": {"command": "npm test ColorPicker", "description": "Run color picker tests"},
                },
            ],
        },
    },
    {
        "type": "user",
        "uuid": "a-result-3",
        "parentUuid": "a-asst-3",
        "sessionId": SESSION_A_ID,
        "timestamp": _ts(6),
        "cwd": "/tmp/canary-app",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "a-tool-3",
                    "content": "PASS  src/ColorPicker.test.tsx (12 tests)",
                }
            ],
        },
        "toolUseResult": {"success": True, "exitCode": 0},
    },
]


# ─── Session B: mixed-topic v2 release notes ────────────────────────────────
#
# This session is mostly about drafting release notes for a v2 release of an
# unrelated tool, but the assistant draft includes a "things v3 should fix"
# subsection that briefly mentions color picker accessibility followups.
# This is the throwaway-mention-in-otherwise-off-topic-session pattern that
# triggered the eef364a9 bug.

SESSION_B_ID = "canary-mixed-release"
SESSION_B_EVENTS = [
    {
        "type": "user",
        "uuid": "b-user-1",
        "parentUuid": None,
        "sessionId": SESSION_B_ID,
        "timestamp": _ts(20),
        "cwd": "/tmp/canary-tool",
        "gitBranch": "main",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": (
                "Draft the v2.0 release notes for our deployment tool. We "
                "shipped the new dashboard, the kubernetes helm charts, and "
                "the SAML SSO integration. Make it markdown, dev-blog style."
            ),
        },
    },
    {
        "type": "assistant",
        "uuid": "b-asst-1",
        "parentUuid": "b-user-1",
        "sessionId": SESSION_B_ID,
        "timestamp": _ts(21),
        "cwd": "/tmp/canary-tool",
        "isSidechain": False,
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": (
                        "Need to write release notes covering dashboard, helm "
                        "charts, and SAML SSO. Should also flag known followups "
                        "honestly, not just marketing copy."
                    ),
                },
                {
                    "type": "text",
                    "text": "I'll draft the release notes.",
                },
                {
                    "type": "tool_use",
                    "id": "b-tool-1",
                    "name": "Write",
                    "input": {
                        "file_path": "/tmp/canary-tool/RELEASE_NOTES_v2.md",
                        "content": (
                            "# v2.0 Release Notes\n\n"
                            "## What's new\n\n"
                            "- **New dashboard**: real-time deploy status, log streaming, env-var diff view.\n"
                            "- **Helm charts**: production-ready Kubernetes deployment with autoscaling.\n"
                            "- **SAML SSO**: enterprise auth via Okta, Auth0, or any SAML 2.0 IdP.\n\n"
                            "## What's still broken\n\n"
                            "Honest list. These will probably be v3.0:\n\n"
                            "- **Color picker accessibility followups.** The settings page color picker still "
                            "needs ARIA improvements — keyboard navigation and screen-reader announcements "
                            "are inconsistent across browsers. Fix is small but cross-browser.\n"
                            "- **Helm rollback is manual.** No `--rollback` flag yet on the CLI.\n"
                            "- **Dashboard log streaming reconnects too aggressively** on flaky networks.\n\n"
                            "## Try it\n\n"
                            "```bash\nnpm install -g our-deploy-tool@2.0.0\n```\n"
                        ),
                    },
                },
            ],
        },
    },
    {
        "type": "user",
        "uuid": "b-result-1",
        "parentUuid": "b-asst-1",
        "sessionId": SESSION_B_ID,
        "timestamp": _ts(22),
        "cwd": "/tmp/canary-tool",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "b-tool-1",
                    "content": "File created successfully.",
                }
            ],
        },
        "toolUseResult": {"success": True},
    },
    {
        "type": "assistant",
        "uuid": "b-asst-2",
        "parentUuid": "b-result-1",
        "sessionId": SESSION_B_ID,
        "timestamp": _ts(23),
        "cwd": "/tmp/canary-tool",
        "isSidechain": False,
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Draft saved to RELEASE_NOTES_v2.md. The 'what's still "
                        "broken' section calls out the color picker accessibility "
                        "work, the missing helm rollback flag, and the dashboard "
                        "log streaming reconnect issue as v3 followups. Ready "
                        "to ship when you are."
                    ),
                },
            ],
        },
    },
]


SESSIONS = [
    ("session_a_focused.jsonl", SESSION_A_EVENTS),
    ("session_b_mixed.jsonl", SESSION_B_EVENTS),
]


ASSERTIONS = [
    # Baseline: the focused session must appear in the rendered narrative for
    # its on-topic query. If this fails, recall is broken at the basic level,
    # not just the secondary-match path.
    RecallAssertion(
        query="color picker accessibility keyboard navigation",
        must_surface_session_id=SESSION_A_ID,
        description="Focused session must surface in narrative for its dense topic",
    ),
    # The bug-class assertion: the mixed-topic session that briefly mentions
    # the queried term must appear in the rendered narrative, not just sit
    # silently in the internal segments array. The pre-fix failure mode was
    # exactly this — segments existed but the renderer dropped them when
    # episodes (or the focused session's segment) won the primary slot.
    RecallAssertion(
        query="color picker accessibility followups for next release",
        must_surface_session_id=SESSION_B_ID,
        description=(
            "Mixed-topic session must appear in rendered narrative even when "
            "a more focused session wins the primary slot"
        ),
    ),
]
