"""Sample corpus generator for `longhand demo`.

Produces 3 fictional Claude Code sessions on a `demo-shop` project,
covering a realistic Stripe-webhook + auth-migration workflow that
exercises the cross-session recall, file history, and project status
features without touching the user's real session data.

Deterministic — same input dir always produces the same JSONL files
(stable session IDs, timestamps, event UUIDs) so the walkthrough
output is reproducible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MODEL = "claude-sonnet-4-6"


def _ts(day_offset: int, hour: int, minute: int) -> str:
    """Deterministic timestamps anchored to 2026-05-15 for the corpus."""
    base_day = 15 + day_offset
    return f"2026-05-{base_day:02d}T{hour:02d}:{minute:02d}:00.000Z"


def _user(uuid: str, parent: str | None, sid: str, ts: str, cwd: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": cwd,
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }


def _assistant(
    uuid: str,
    parent: str,
    sid: str,
    ts: str,
    cwd: str,
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": cwd,
        "isSidechain": False,
        "message": {"model": _MODEL, "role": "assistant", "content": blocks},
    }


def _tool_result(
    uuid: str, parent: str, sid: str, ts: str, cwd: str, tool_use_id: str, result: str
) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": cwd,
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": result}],
        },
        "toolUseResult": {"success": True, "stdout": result},
    }


def generate_corpus(project_dir: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Generate the 3-session demo corpus.

    Args:
        project_dir: absolute path to the fictional demo-shop project root.
            Must exist and contain a `.git` marker so project inference
            attributes events correctly. Caller is responsible for creating it.

    Returns:
        List of (filename, events) tuples ready to write as JSONL and
        ingest. Filenames are session-id based (`session-<id>.jsonl`).
    """
    pd = str(project_dir)
    api_route = f"{pd}/src/app/api/webhooks/stripe/route.ts"
    auth_lib = f"{pd}/src/lib/supabase.ts"
    middleware = f"{pd}/src/middleware.ts"
    checkout_route = f"{pd}/src/app/api/checkout/route.ts"

    # ──────────────────────────────────────────────────────────────────
    # Session A — Add Stripe webhook handler. Hits a signature bug, fixes.
    # ──────────────────────────────────────────────────────────────────
    sid_a = "demo-001-stripe-webhook"
    session_a = [
        _user(
            "a-u1",
            None,
            sid_a,
            _ts(0, 14, 0),
            pd,
            "Add a Stripe webhook handler at /api/webhooks/stripe that verifies signatures and logs payment_intent.succeeded events.",
        ),
        _assistant(
            "a-a1",
            "a-u1",
            sid_a,
            _ts(0, 14, 1),
            pd,
            [
                {
                    "type": "text",
                    "text": "I'll create the webhook route. First reading existing API structure.",
                },
                {
                    "type": "tool_use",
                    "id": "a-t1",
                    "name": "Read",
                    "input": {"file_path": f"{pd}/src/app/api/checkout/route.ts"},
                },
            ],
        ),
        _tool_result(
            "a-r1",
            "a-a1",
            sid_a,
            _ts(0, 14, 1),
            pd,
            "a-t1",
            "import Stripe from 'stripe'\nimport { NextResponse } from 'next/server'\n\nconst stripe = new Stripe(process.env.STRIPE_SECRET_KEY!)\n\nexport async function POST(req: Request) {\n  // ... checkout session creation\n}",
        ),
        _assistant(
            "a-a2",
            "a-r1",
            sid_a,
            _ts(0, 14, 3),
            pd,
            [
                {
                    "type": "text",
                    "text": "Good — Stripe client pattern is set up. Writing the webhook handler.",
                },
                {
                    "type": "tool_use",
                    "id": "a-t2",
                    "name": "Write",
                    "input": {
                        "file_path": api_route,
                        "content": "import Stripe from 'stripe'\nimport { NextResponse } from 'next/server'\n\nconst stripe = new Stripe(process.env.STRIPE_SECRET_KEY!)\n\nexport async function POST(req: Request) {\n  const sig = req.headers.get('stripe-signature')\n  const body = await req.text()\n  const event = stripe.webhooks.constructEvent(body, sig!, process.env.STRIPE_WEBHOOK_SECRET!)\n  if (event.type === 'payment_intent.succeeded') {\n    console.log('Payment succeeded:', event.data.object.id)\n  }\n  return NextResponse.json({ received: true })\n}",
                    },
                },
            ],
        ),
        _tool_result(
            "a-r2", "a-a2", sid_a, _ts(0, 14, 3), pd, "a-t2", f"File created at {api_route}"
        ),
        _assistant(
            "a-a3",
            "a-r2",
            sid_a,
            _ts(0, 14, 5),
            pd,
            [
                {
                    "type": "text",
                    "text": "Now testing signature verification with a local payload.",
                },
                {
                    "type": "tool_use",
                    "id": "a-t3",
                    "name": "Bash",
                    "input": {"command": "cd " + pd + " && npm test webhooks"},
                },
            ],
        ),
        _tool_result(
            "a-r3",
            "a-a3",
            sid_a,
            _ts(0, 14, 6),
            pd,
            "a-t3",
            "FAIL  test/webhooks.test.ts\n  Stripe webhook > verifies valid signature\n    Error: No signatures found matching the expected signature for payload",
        ),
        _assistant(
            "a-a4",
            "a-r3",
            sid_a,
            _ts(0, 14, 8),
            pd,
            [
                {
                    "type": "text",
                    "text": "Signature check is failing because constructEvent needs the RAW body — req.text() in Next.js App Router is already decoded. Switching to req.arrayBuffer() then converting to Buffer.",
                },
                {
                    "type": "tool_use",
                    "id": "a-t4",
                    "name": "Edit",
                    "input": {
                        "file_path": api_route,
                        "old_string": "  const body = await req.text()",
                        "new_string": "  const body = Buffer.from(await req.arrayBuffer())",
                    },
                },
            ],
        ),
        _tool_result("a-r4", "a-a4", sid_a, _ts(0, 14, 8), pd, "a-t4", "File updated"),
        _assistant(
            "a-a5",
            "a-r4",
            sid_a,
            _ts(0, 14, 10),
            pd,
            [
                {
                    "type": "tool_use",
                    "id": "a-t5",
                    "name": "Bash",
                    "input": {"command": "cd " + pd + " && npm test webhooks"},
                },
            ],
        ),
        _tool_result(
            "a-r5",
            "a-a5",
            sid_a,
            _ts(0, 14, 11),
            pd,
            "a-t5",
            "PASS test/webhooks.test.ts\n  Stripe webhook > verifies valid signature ✓\n  Stripe webhook > logs payment_intent.succeeded ✓",
        ),
        _assistant(
            "a-a6",
            "a-r5",
            sid_a,
            _ts(0, 14, 12),
            pd,
            [
                {
                    "type": "text",
                    "text": "Tests pass. The fix: use arrayBuffer() not text() because Stripe needs raw bytes for HMAC verification.",
                },
            ],
        ),
    ]

    # ──────────────────────────────────────────────────────────────────
    # Session B — Migrate auth from Supabase v1 to v2 SSR pattern.
    # ──────────────────────────────────────────────────────────────────
    sid_b = "demo-002-auth-migration"
    session_b = [
        _user(
            "b-u1",
            None,
            sid_b,
            _ts(1, 9, 30),
            pd,
            "Migrate the auth client from @supabase/supabase-js createClient to the @supabase/ssr createServerClient pattern for App Router.",
        ),
        _assistant(
            "b-a1",
            "b-u1",
            sid_b,
            _ts(1, 9, 31),
            pd,
            [
                {
                    "type": "tool_use",
                    "id": "b-t1",
                    "name": "Read",
                    "input": {"file_path": auth_lib},
                },
            ],
        ),
        _tool_result(
            "b-r1",
            "b-a1",
            sid_b,
            _ts(1, 9, 31),
            pd,
            "b-t1",
            "import { createClient } from '@supabase/supabase-js'\n\nexport const supabase = createClient(\n  process.env.NEXT_PUBLIC_SUPABASE_URL!,\n  process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!\n)",
        ),
        _assistant(
            "b-a2",
            "b-r1",
            sid_b,
            _ts(1, 9, 33),
            pd,
            [
                {
                    "type": "text",
                    "text": "Replacing with the SSR createServerClient that reads/writes auth cookies properly.",
                },
                {
                    "type": "tool_use",
                    "id": "b-t2",
                    "name": "Write",
                    "input": {
                        "file_path": auth_lib,
                        "content": "import { createServerClient } from '@supabase/ssr'\nimport { cookies } from 'next/headers'\n\nexport async function getSupabaseServerClient() {\n  const cookieStore = await cookies()\n  return createServerClient(\n    process.env.NEXT_PUBLIC_SUPABASE_URL!,\n    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,\n    {\n      cookies: {\n        getAll() { return cookieStore.getAll() },\n        setAll(toSet) { toSet.forEach(({ name, value, options }) => cookieStore.set(name, value, options)) },\n      },\n    }\n  )\n}",
                    },
                },
            ],
        ),
        _tool_result("b-r2", "b-a2", sid_b, _ts(1, 9, 33), pd, "b-t2", "File updated"),
        _assistant(
            "b-a3",
            "b-r2",
            sid_b,
            _ts(1, 9, 35),
            pd,
            [
                {
                    "type": "text",
                    "text": "Note: all callers of the old `supabase` export need to switch to `await getSupabaseServerClient()`. Searching for usages.",
                },
                {
                    "type": "tool_use",
                    "id": "b-t3",
                    "name": "Grep",
                    "input": {"pattern": "from '@/lib/supabase'", "path": pd},
                },
            ],
        ),
        _tool_result(
            "b-r3", "b-a3", sid_b, _ts(1, 9, 35), pd, "b-t3", f"{middleware}:3\n{checkout_route}:1"
        ),
        _assistant(
            "b-a4",
            "b-r3",
            sid_b,
            _ts(1, 9, 37),
            pd,
            [
                {
                    "type": "text",
                    "text": "Two callers. Middleware first — it runs on every request, so the change matters most there.",
                },
                {
                    "type": "tool_use",
                    "id": "b-t4",
                    "name": "Edit",
                    "input": {
                        "file_path": middleware,
                        "old_string": "import { supabase } from '@/lib/supabase'",
                        "new_string": "import { getSupabaseServerClient } from '@/lib/supabase'",
                    },
                },
            ],
        ),
        _tool_result("b-r4", "b-a4", sid_b, _ts(1, 9, 37), pd, "b-t4", "File updated"),
    ]

    # ──────────────────────────────────────────────────────────────────
    # Session C — Quick fix for 401 on /api/checkout (caused by B).
    # ──────────────────────────────────────────────────────────────────
    sid_c = "demo-003-checkout-401"
    session_c = [
        _user(
            "c-u1",
            None,
            sid_c,
            _ts(2, 11, 0),
            pd,
            "/api/checkout is returning 401 for logged-in users. It worked yesterday.",
        ),
        _assistant(
            "c-a1",
            "c-u1",
            sid_c,
            _ts(2, 11, 1),
            pd,
            [
                {
                    "type": "text",
                    "text": "The auth migration yesterday changed the import. Checking if /api/checkout was updated.",
                },
                {
                    "type": "tool_use",
                    "id": "c-t1",
                    "name": "Read",
                    "input": {"file_path": checkout_route},
                },
            ],
        ),
        _tool_result(
            "c-r1",
            "c-a1",
            sid_c,
            _ts(2, 11, 1),
            pd,
            "c-t1",
            "import { supabase } from '@/lib/supabase'\n\nexport async function POST(req: Request) {\n  const { data: { user } } = await supabase.auth.getUser()\n  if (!user) return new Response('Unauthorized', { status: 401 })\n  // ...\n}",
        ),
        _assistant(
            "c-a2",
            "c-r1",
            sid_c,
            _ts(2, 11, 3),
            pd,
            [
                {
                    "type": "text",
                    "text": "Confirmed — checkout still imports the old `supabase` (which is undefined now), so `supabase.auth.getUser()` throws and 401 is the fallback. Fixing.",
                },
                {
                    "type": "tool_use",
                    "id": "c-t2",
                    "name": "Edit",
                    "input": {
                        "file_path": checkout_route,
                        "old_string": "import { supabase } from '@/lib/supabase'\n\nexport async function POST(req: Request) {\n  const { data: { user } } = await supabase.auth.getUser()",
                        "new_string": "import { getSupabaseServerClient } from '@/lib/supabase'\n\nexport async function POST(req: Request) {\n  const supabase = await getSupabaseServerClient()\n  const { data: { user } } = await supabase.auth.getUser()",
                    },
                },
            ],
        ),
        _tool_result("c-r2", "c-a2", sid_c, _ts(2, 11, 3), pd, "c-t2", "File updated"),
        _assistant(
            "c-a3",
            "c-r2",
            sid_c,
            _ts(2, 11, 5),
            pd,
            [
                {
                    "type": "text",
                    "text": "Fixed. The pattern from yesterday's migration needed to propagate to this caller too.",
                },
            ],
        ),
    ]

    return [
        (f"{sid_a}.jsonl", session_a),
        (f"{sid_b}.jsonl", session_b),
        (f"{sid_c}.jsonl", session_c),
    ]
