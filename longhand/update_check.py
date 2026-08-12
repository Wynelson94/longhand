"""Best-effort PyPI freshness check for the interactive CLI.

Design constraints (v0.12 audit plan, "update channel"):

- **Structurally excluded from hooks and the MCP server.** The only caller is
  the CLI app callback in ``cli/_commands.py``, which skips every hidden
  plumbing command (``ingest-session``, ``ingest-live``, ``mcp-server``,
  ``__prompt-hook-run``, ...). Nothing in the hook or MCP code paths imports
  this module's network functions.
- **The hint never touches the network.** ``hint_from_cache`` reads the cache
  file only; the network refresh happens after a command has already produced
  its output, so a slow or offline PyPI can never delay real work.
- **Never raises.** A missing, corrupt, or unwritable cache, a network
  failure, or an unparseable version string all degrade to "no hint".
- **Opt-out**: ``LONGHAND_NO_UPDATE_CHECK=1`` disables reads, hints, and the
  network refresh entirely.

The only data transmitted is the HTTPS request to pypi.org itself — no
telemetry, no identifiers, nothing about the corpus.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from longhand.version import __version__

CACHE_TTL_SECONDS = 24 * 60 * 60
PYPI_JSON_URL = "https://pypi.org/pypi/longhand/json"
_TIMEOUT_SECONDS = 2.0

# Why the last refresh failed, so doctor can report the cause instead of
# guessing one. "offline?" was wrong for the dominant real-world case: on a
# python.org macOS install, urllib verifies against OpenSSL's own trust store
# rather than the system keychain, so pypi.org fails with
# CERTIFICATE_VERIFY_FAILED while `curl` to the same URL succeeds. Sending
# that user to debug their network is a Promise 5 violation.
_last_failure: str | None = None


def last_failure() -> str | None:
    """Failure class of the most recent refresh: 'tls-trust', 'unreachable',
    'bad-payload', or None if the last attempt succeeded or never ran."""
    return _last_failure


def _ssl_context() -> ssl.SSLContext:
    """Verify against certifi's CA bundle when it is importable.

    certifi is present transitively (chromadb pulls requests/httpx), but this
    stays a soft import: if it ever goes away we fall back to the default
    context and report the honest failure rather than breaking.

    This exists because a python.org macOS install verifies against OpenSSL's
    own trust store, not the system keychain — so pypi.org fails with
    CERTIFICATE_VERIFY_FAILED while `curl` to the same URL succeeds.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def is_disabled() -> bool:
    return os.environ.get("LONGHAND_NO_UPDATE_CHECK", "").strip() not in ("", "0")


def cache_path(data_dir: str | Path | None = None) -> Path:
    # Imported lazily: store pulls in chromadb-adjacent modules that this
    # module must not load on the fast --version path.
    from longhand.storage.store import resolve_data_dir

    base = resolve_data_dir(data_dir)
    return base / "update-check.json"


def read_cache(data_dir: str | Path | None = None) -> dict | None:
    """Return ``{"latest": str, "checked_at": float}`` or None. Never raises."""
    try:
        payload = json.loads(cache_path(data_dir).read_text(encoding="utf-8"))
        if isinstance(payload.get("latest"), str) and isinstance(
            payload.get("checked_at"), (int, float)
        ):
            return payload
    except Exception:
        pass
    return None


def _parse_version(text: str) -> tuple[int, ...] | None:
    """X.Y.Z → (X, Y, Z). Local/dev suffixes are stripped; garbage → None."""
    core = text.split("+")[0].strip()
    parts = core.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def newer_available(installed: str, latest: str) -> bool:
    """True only when both versions parse and latest is strictly newer."""
    a = _parse_version(installed)
    b = _parse_version(latest)
    if a is None or b is None:
        return False
    if a == (0, 0, 0):
        # Uninstalled/dev checkout (version.py falls back to "0.0.0+local") —
        # never nag developers running from a repo.
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


def hint_from_cache(data_dir: str | Path | None = None) -> str | None:
    """One-line upgrade hint from the cache file only — no network, ever."""
    if is_disabled():
        return None
    cached = read_cache(data_dir)
    if cached and newer_available(__version__, cached["latest"]):
        return (
            f"longhand {cached['latest']} is available "
            f"(installed {__version__}) — pip install -U longhand"
        )
    return None


def refresh(data_dir: str | Path | None = None, *, force: bool = False) -> str | None:
    """Fetch the latest version from PyPI and cache it. Never raises.

    Respects the 24h TTL unless ``force``; returns the latest version string
    when a fetch (or fresh cache) is available, else None.
    """
    if is_disabled():
        return None
    cached = read_cache(data_dir)
    if not force and cached and time.time() - cached["checked_at"] < CACHE_TTL_SECONDS:
        return cached["latest"]
    global _last_failure
    try:
        with urllib.request.urlopen(
            PYPI_JSON_URL, timeout=_TIMEOUT_SECONDS, context=_ssl_context()
        ) as resp:
            latest = json.load(resp)["info"]["version"]
        if not isinstance(latest, str):
            _last_failure = "bad-payload"
            return None
    except Exception as e:
        _last_failure = _classify(e)
        return None
    _last_failure = None
    _write_cache(data_dir, latest)
    return latest


def _classify(exc: BaseException) -> str:
    """Map a refresh exception to a failure class doctor can act on.

    Checks the whole cause chain: urllib wraps the SSL error in a URLError,
    so the certificate failure is one `.reason` deep.
    """
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        if isinstance(cur, ssl.SSLError):
            return "tls-trust"
        cur = getattr(cur, "reason", None) or cur.__cause__ or cur.__context__
    if "certificate" in str(exc).lower():
        return "tls-trust"
    return "unreachable"


def _write_cache(data_dir: str | Path | None, latest: str) -> None:
    """Atomic tempfile + rename write, mirroring drift_cache. Never raises."""
    target = cache_path(data_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".update-check-", suffix=".json.tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"latest": latest, "checked_at": time.time()}, f)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    except Exception:
        pass


def after_command(data_dir: str | Path | None = None) -> None:
    """Post-command hook for interactive CLI runs. Never raises.

    Prints the cache-only hint (TTY only, so piped output stays clean), then
    refreshes the cache for the *next* run — the current command's output has
    already been delivered, so the network wait costs the user nothing.
    """
    try:
        if is_disabled():
            return
        hint = hint_from_cache(data_dir)
        if hint and sys.stdout.isatty():
            from rich.console import Console

            Console(stderr=True).print(hint, style="dim")
        refresh(data_dir)
    except Exception:
        pass


def _failure_line(why: str | None) -> str:
    """Name the failure and give a remedy that matches it.

    "offline?" is only honest when the network is actually the problem. A
    trust-store failure has a completely different fix, and telling that user
    to check their connection wastes their time.
    """
    if why == "tls-trust":
        hint = (
            "[yellow]⚠[/yellow] pypi.org TLS certificate verification failed — "
            "your Python cannot verify HTTPS certificates"
        )
        if sys.platform == "darwin":
            return (
                f"{hint}; run [bold]Install Certificates.command[/bold] in your "
                "Python.app folder, or [bold]pip install -U certifi[/bold]"
            )
        return f"{hint}; try [bold]pip install -U certifi[/bold]"
    if why == "bad-payload":
        return "[yellow]⚠[/yellow] pypi.org returned an unexpected response"
    return "[yellow]⚠[/yellow] could not reach pypi.org (offline?)"


def doctor_status(data_dir: str | Path | None = None) -> str:
    """Rich-markup status line for the doctor table. Refreshes (forced)."""
    if is_disabled():
        return "[dim]disabled (LONGHAND_NO_UPDATE_CHECK)[/dim]"
    latest = refresh(data_dir, force=True)
    if latest is None:
        why = last_failure()
        cached = read_cache(data_dir)
        if cached is None:
            return _failure_line(why)
        age_days = max(0.0, (time.time() - cached["checked_at"]) / 86400)
        latest = cached["latest"]
        reason = "TLS certificate verification failed" if why == "tls-trust" else "unreachable"
        suffix = f" [dim](cached {age_days:.0f}d ago; pypi.org {reason})[/dim]"
    else:
        suffix = ""
    if newer_available(__version__, latest):
        return (
            f"[yellow]⚠[/yellow] {latest} available (installed {__version__}) — "
            f"run [bold]pip install -U longhand[/bold]{suffix}"
        )
    return f"[green]✓[/green] up to date ({__version__}){suffix}"
