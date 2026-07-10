"""
Opt-in secret redaction for ingested transcript content.

Claude Code transcripts preserve everything — including any API key or
credential that ever appeared in a prompt or tool output. When enabled
(``longhand config --set redact.enabled=true``), secret-shaped strings
are masked at parse time, before they reach SQLite, the raw JSON blobs,
or the vector index. ``longhand redact`` handles data that was ingested
before the flag was turned on.

Detection is pattern-based, not exhaustive: it catches well-known key
formats (AWS, GitHub, Anthropic, OpenAI, Slack, Stripe, JWTs, DB URLs
with passwords, SSNs, plausible credit cards), not arbitrary secrets.

Patterns ported from Ledger (src/ledger/audit/secrets.py) — same author,
shared lineage. Raw matched values are never logged, printed, or stored;
masks keep only the first/last 4 characters and the length.
"""

from __future__ import annotations

import json
import re
from typing import Any

from longhand.types import Event

# (name, pattern) — order matters: anthropic before the generic sk- catch-all.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "aws_secret_access_key",
        re.compile(r"(?i)aws(.{0,20})?(secret|sk).{0,5}[:=]\s*[\"']?([A-Za-z0-9/+=]{40})[\"']?"),
    ),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b")),
    ("openai_key", re.compile(r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[abpors]-[A-Za-z0-9-]{10,}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{24,}\b")),
    (
        "ssh_private_key",
        re.compile(r"-----BEGIN (?:RSA|EC|OPENSSH|DSA|PGP) PRIVATE KEY-----"),
    ),
    (
        "jwt_token",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "db_url_with_password",
        re.compile(
            r"\b(?:postgres|postgresql|mysql|mongodb)(?:\+[a-z]+)?://[^:\s]+:[^@\s]+@[^\s\"']+"
        ),
    ),
    ("ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    # Deliberately loose; _is_plausible_card() (Luhn + BIN + variety) does
    # the real filtering so test/placeholder numbers don't get masked.
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
]


_CARD_PRECHECK = re.compile(r"\d(?:[ -]?\d){12}")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_plausible_card(raw: str) -> bool:
    digits = re.sub(r"[ -]", "", raw)
    if not 13 <= len(digits) <= 16:
        return False
    if digits[0] not in "3456":
        return False
    if len(set(digits)) < 3:
        return False
    return _luhn_ok(digits)


def _mask(value: str) -> str:
    """Keep first/last 4 chars + length; never enough to reconstruct."""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} (len={len(value)})"


def redact_text(text: str) -> tuple[str, int]:
    """Mask all secret-shaped substrings. Returns (new_text, match_count)."""
    if not text:
        return text, 0
    count = 0

    for name, pattern in PATTERNS:
        # The card pattern is the CPU hog on numeric-heavy transcripts; a
        # cheap pre-check (is there any 13-digit-ish run at all?) skips it
        # for the overwhelmingly common case.
        if name == "credit_card" and not _CARD_PRECHECK.search(text):
            continue

        def _sub(m: re.Match[str], _name: str = name) -> str:
            nonlocal count
            value = m.group(0)
            if _name == "credit_card" and not _is_plausible_card(value):
                return value
            count += 1
            return _mask(value)

        text = pattern.sub(_sub, text)
    return text, count


def scan_text(text: str | None) -> dict[str, int]:
    """Count matches per pattern WITHOUT modifying or returning values."""
    counts: dict[str, int] = {}
    if not text:
        return counts
    for name, pattern in PATTERNS:
        n = 0
        for m in pattern.finditer(text):
            if name == "credit_card" and not _is_plausible_card(m.group(0)):
                continue
            n += 1
        if n:
            counts[name] = n
    return counts


def _redact_obj(obj: Any) -> tuple[Any, int]:
    """Recursively redact every string leaf in a dict/list structure."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        total = 0
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            new_v, n = _redact_obj(v)
            out[k] = new_v
            total += n
        return out, total
    if isinstance(obj, list):
        total = 0
        items: list[Any] = []
        for v in obj:
            new_v, n = _redact_obj(v)
            items.append(new_v)
            total += n
        return items, total
    return obj, 0


_EVENT_TEXT_FIELDS = ("content", "tool_output", "error_snippet", "old_content", "new_content")


def redact_event(event: Event) -> int:
    """Mask secrets across an Event in place, including the raw JSON dict.

    raw is forensic-by-design, but a masked raw beats a leaked key — that
    trade is exactly what opting in means.
    """
    total = 0
    for field in _EVENT_TEXT_FIELDS:
        val = getattr(event, field)
        if val:
            new, n = redact_text(val)
            if n:
                setattr(event, field, new)
                total += n
    if event.tool_input:
        event.tool_input, n = _redact_obj(event.tool_input)
        total += n
    if event.raw:
        event.raw, n = _redact_obj(event.raw)
        total += n
    return total


def redaction_enabled() -> bool:
    """Read redact.enabled from ~/.longhand/config.json. Default: off."""
    from pathlib import Path

    config_path = Path.home() / ".longhand" / "config.json"
    try:
        if config_path.exists():
            user = json.loads(config_path.read_text())
            if isinstance(user, dict):
                redact_cfg = user.get("redact")
                if isinstance(redact_cfg, dict):
                    return bool(redact_cfg.get("enabled", False))
    except Exception:
        pass
    return False
