"""Tests for the PyPI update check: cache semantics, hints, doctor line, and
the structural guarantee that hooks and the MCP server never touch it."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from longhand import update_check
from longhand.cli._commands import _UPDATE_CHECK_EXCLUDED, app
from longhand.version import __version__

runner = CliRunner()


def _network_forbidden(*args, **kwargs):
    raise AssertionError("update check touched the network when it must not")


@pytest.fixture
def enabled(monkeypatch, tmp_path: Path) -> Path:
    """Enable the check (conftest disables it suite-wide) with an isolated
    cache under tmp_path. Returns the data dir the cache lives in."""
    monkeypatch.delenv("LONGHAND_NO_UPDATE_CHECK", raising=False)
    import longhand.storage.store as store_mod

    data_dir = tmp_path / "lh"
    monkeypatch.setattr(store_mod, "DEFAULT_DATA_DIR", data_dir)
    return data_dir


def _seed_cache(data_dir: Path, latest: str, age_seconds: float = 0.0) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "update-check.json").write_text(
        json.dumps({"latest": latest, "checked_at": time.time() - age_seconds})
    )


# ─── version comparison ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("installed", "latest", "expected"),
    [
        ("0.11.2", "0.12.0", True),
        ("0.11.2", "0.11.2", False),
        ("0.12.0", "0.11.9", False),
        ("0.11.2", "0.11.10", True),  # numeric compare, not lexicographic
        ("0.11.2", "1.0", True),  # mixed lengths pad with zeros
        ("1.0", "1.0.0", False),
        ("0.0.0+local", "0.11.2", False),  # dev checkout — never nag
        ("garbage", "0.12.0", False),
        ("0.11.2", "not-a-version", False),
    ],
)
def test_newer_available(installed: str, latest: str, expected: bool):
    assert update_check.newer_available(installed, latest) is expected


# ─── --version flag ──────────────────────────────────────────────────────────


def test_version_flag_prints_version_without_network(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"longhand {__version__}" in result.stdout


def test_version_flag_shows_upgrade_hint_from_cache(enabled, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)
    _seed_cache(enabled, "99.0.0")
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "99.0.0" in result.stdout
    assert "pip install -U longhand" in result.stdout


# ─── cache-only hint ─────────────────────────────────────────────────────────


def test_hint_none_without_cache(enabled):
    assert update_check.hint_from_cache() is None


def test_hint_none_on_corrupt_cache(enabled):
    enabled.mkdir(parents=True, exist_ok=True)
    (enabled / "update-check.json").write_text("{not json")
    assert update_check.hint_from_cache() is None


def test_hint_none_when_up_to_date(enabled):
    _seed_cache(enabled, __version__)
    assert update_check.hint_from_cache() is None


def test_hint_when_newer_cached(enabled):
    _seed_cache(enabled, "99.0.0")
    hint = update_check.hint_from_cache()
    assert hint is not None and "99.0.0" in hint


# ─── refresh: TTL, network failure, opt-out ──────────────────────────────────


def test_refresh_fresh_cache_skips_network(enabled, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)
    _seed_cache(enabled, "0.11.9", age_seconds=60)
    assert update_check.refresh() == "0.11.9"


def test_refresh_expired_cache_fetches_and_writes(enabled, monkeypatch):
    _seed_cache(enabled, "0.11.9", age_seconds=update_check.CACHE_TTL_SECONDS + 60)
    body = io.StringIO(json.dumps({"info": {"version": "0.12.5"}}))
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout, context=None: body)
    assert update_check.refresh() == "0.12.5"
    cached = update_check.read_cache()
    assert cached is not None and cached["latest"] == "0.12.5"


def test_refresh_network_error_returns_none_and_keeps_cache(enabled, monkeypatch):
    _seed_cache(enabled, "0.11.9", age_seconds=update_check.CACHE_TTL_SECONDS + 60)

    def _down(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _down)
    assert update_check.refresh(force=True) is None
    cached = update_check.read_cache()
    assert cached is not None and cached["latest"] == "0.11.9"  # untouched


def test_disabled_env_blocks_hint_and_refresh(monkeypatch, tmp_path):
    # conftest sets LONGHAND_NO_UPDATE_CHECK=1; only isolate the data dir.
    import longhand.storage.store as store_mod

    data_dir = tmp_path / "lh"
    monkeypatch.setattr(store_mod, "DEFAULT_DATA_DIR", data_dir)
    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)
    _seed_cache(data_dir, "99.0.0")

    assert update_check.is_disabled() is True
    assert update_check.hint_from_cache() is None
    assert update_check.refresh(force=True) is None


# ─── after_command (the CLI post-run hook) ───────────────────────────────────


def test_after_command_prints_hint_on_tty(enabled, monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)
    _seed_cache(enabled, "99.0.0")  # fresh cache: refresh stays offline
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    update_check.after_command()
    assert "99.0.0" in capsys.readouterr().err  # hint goes to stderr, dim


def test_after_command_silent_when_piped(enabled, monkeypatch, capsys):
    monkeypatch.setattr("urllib.request.urlopen", _network_forbidden)
    _seed_cache(enabled, "99.0.0")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    update_check.after_command()
    captured = capsys.readouterr()
    assert "99.0.0" not in captured.out
    assert "99.0.0" not in captured.err


def test_after_command_never_raises(enabled, monkeypatch):
    monkeypatch.setattr(update_check, "hint_from_cache", _network_forbidden)  # any internal failure
    update_check.after_command()  # must swallow, not raise


def test_cache_path_honors_data_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LONGHAND_DATA_DIR", str(tmp_path / "env"))
    assert update_check.cache_path() == tmp_path / "env" / "update-check.json"


# ─── structural exclusion: hooks and MCP never run the check ─────────────────


def test_every_hidden_command_is_excluded():
    """Hooks and the MCP server enter through hidden plumbing commands; any
    new hidden command must be added to _UPDATE_CHECK_EXCLUDED explicitly."""
    for info in app.registered_commands:
        name = info.name or info.callback.__name__.replace("_", "-")
        if info.hidden:
            assert name in _UPDATE_CHECK_EXCLUDED, (
                f"hidden command {name!r} is not excluded from the update check"
            )


def test_hook_entry_point_never_schedules_update_check(monkeypatch, tmp_path):
    calls: list[int] = []
    monkeypatch.setattr(update_check, "after_command", lambda *a, **k: calls.append(1))
    runner.invoke(app, ["ingest-session", str(tmp_path / "missing.jsonl")])
    assert calls == []


def test_interactive_command_schedules_update_check(monkeypatch, tmp_path):
    calls: list[int] = []
    monkeypatch.setattr(update_check, "after_command", lambda *a, **k: calls.append(1))
    result = runner.invoke(app, ["projects", "--data-dir", str(tmp_path / "lh")])
    assert result.exit_code == 0
    assert calls == [1]


# ─── doctor line ─────────────────────────────────────────────────────────────


def test_doctor_status_up_to_date(enabled, monkeypatch):
    monkeypatch.setattr(update_check, "refresh", lambda *a, **k: __version__)
    assert "up to date" in update_check.doctor_status()


def test_doctor_status_update_available(enabled, monkeypatch):
    monkeypatch.setattr(update_check, "refresh", lambda *a, **k: "99.0.0")
    line = update_check.doctor_status()
    assert "99.0.0" in line and "pip install -U longhand" in line


def test_doctor_status_offline_without_cache(enabled, monkeypatch):
    monkeypatch.setattr(update_check, "refresh", lambda *a, **k: None)
    assert "could not reach" in update_check.doctor_status()


# ─── failure class is reported, not guessed (Promise 5) ─────────────────────
#
# On a python.org macOS install, urllib verifies against OpenSSL's trust store
# rather than the system keychain, so pypi.org fails with
# CERTIFICATE_VERIFY_FAILED while `curl` to the same URL succeeds. Reporting
# that as "offline?" sends the user to debug a network that is fine. Longhand
# ran with this misdiagnosis for weeks and it was logged as "unexplained".


def test_refresh_records_the_failure_class(enabled, monkeypatch):
    import ssl

    def _boom(*a, **k):
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", _boom)

    assert update_check.refresh(enabled, force=True) is None
    assert update_check.last_failure() == "tls-trust"


def test_refresh_distinguishes_a_real_network_failure(enabled, monkeypatch):
    def _boom(*a, **k):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", _boom)

    assert update_check.refresh(enabled, force=True) is None
    assert update_check.last_failure() == "unreachable"


def test_doctor_status_names_the_trust_store_problem(enabled, monkeypatch):
    import ssl

    def _boom(*a, **k):
        raise ssl.SSLCertVerificationError("certificate verify failed")

    monkeypatch.setattr(update_check.urllib.request, "urlopen", _boom)

    line = update_check.doctor_status(enabled)
    assert "offline" not in line.lower(), "a TLS trust failure is not being offline"
    assert "certificate" in line.lower()
    # The remedy must be actionable and specific to the real cause.
    assert "Install Certificates" in line or "certifi" in line


def test_refresh_succeeds_through_a_certifi_bundle(enabled, monkeypatch):
    """The fix, not just the message: verify against certifi when it is there."""
    pytest.importorskip("certifi")
    seen: dict = {}

    class _Resp:
        def read(self):
            return b'{"info": {"version": "9.9.9"}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(url, timeout=None, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(update_check.urllib.request, "urlopen", _fake)
    monkeypatch.setattr(update_check.json, "load", lambda r: {"info": {"version": "9.9.9"}})

    assert update_check.refresh(enabled, force=True) == "9.9.9"
    assert seen["context"] is not None, "urlopen was called without an explicit SSL context"


def test_doctor_status_offline_falls_back_to_stale_cache(enabled, monkeypatch):
    monkeypatch.setattr(update_check, "refresh", lambda *a, **k: None)
    _seed_cache(enabled, "99.0.0", age_seconds=3 * 86400)
    line = update_check.doctor_status()
    assert "99.0.0" in line and "cached" in line


def test_doctor_status_disabled(monkeypatch):
    # conftest keeps LONGHAND_NO_UPDATE_CHECK=1 for this test.
    assert "disabled" in update_check.doctor_status()
