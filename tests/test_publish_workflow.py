"""The release workflow publishes to the MCP Registry only after PyPI.

These checks pin what makes that safe: the registry job waits for the PyPI job
(which waits for a human approval on the `pypi` environment), signs in with
GitHub OIDC instead of a stored secret, and runs a pinned `mcp-publisher`
whose signature is checked before it runs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _runs(job: dict[str, Any]) -> list[str]:
    return [step.get("run") or "" for step in job.get("steps", [])]


def _registry_job() -> tuple[str, dict[str, Any]]:
    found = [
        (name, job)
        for name, job in _jobs().items()
        if any("mcp-publisher publish" in run for run in _runs(job))
    ]
    assert len(found) == 1, f"expected one job that runs mcp-publisher publish, got {found}"
    return found[0]


def test_registry_publish_waits_for_the_approved_pypi_publish():
    pypi_jobs = [name for name, job in _jobs().items() if job.get("environment") == "pypi"]
    assert len(pypi_jobs) == 1
    _, job = _registry_job()
    needs = job.get("needs") or []
    needs = [needs] if isinstance(needs, str) else list(needs)
    assert pypi_jobs[0] in needs


def test_registry_publish_signs_in_with_github_oidc():
    _, job = _registry_job()
    assert job["permissions"].get("id-token") == "write"
    assert any("mcp-publisher login github-oidc" in run for run in _runs(job))


def test_mcp_publisher_is_pinned_and_verified_before_it_runs():
    every_run = [run for job in _jobs().values() for run in _runs(job)]
    assert not any("releases/latest" in run for run in every_run)
    _, job = _registry_job()
    assert re.fullmatch(r"v\d+\.\d+\.\d+", job["env"]["MCP_PUBLISHER_VERSION"])
    runs = _runs(job)
    verify = next(i for i, run in enumerate(runs) if "cosign verify-blob" in run)
    first_use = next(i for i, run in enumerate(runs) if "./mcp-publisher" in run)
    assert verify < first_use
