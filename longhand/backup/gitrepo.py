"""Argv-only git wrapper for the backup repository.

Every call is a list-form argv, never a shell string, and carries
``-c protocol.ext.allow=never`` so a remote can never name a transport helper
that runs commands. ``--`` precedes every remote or path the user supplied.
Failures surface as :class:`GitError`; credentials embedded in a URL
(``https://user:token@host/…``) are masked in its text so nothing key-shaped
reaches a terminal or a log through a git message.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULT_BRANCH = "main"
IDENTITY_NAME = "Longhand backup"
IDENTITY_EMAIL = "backup@longhand.invalid"

_EXT_GUARD = ("-c", "protocol.ext.allow=never")
_GH_TIMEOUT_SECONDS = 60

_URL_RE = re.compile(r"^(?:https|ssh|file)://\S+$")
_SCP_RE = re.compile(r"^[\w.-]+@[\w.-]+:\S+$")
_USERINFO_RE = re.compile(r"://[^/@\s]+@")
_GITHUB_RE = re.compile(
    r"^(?:https://(?:[^/@\s]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


def redact_remote(remote: str) -> str:
    """Mask ``scheme://user:token@host`` userinfo; ``git@host:path`` is left alone."""
    return _USERINFO_RE.sub("://***@", remote)


class GitError(RuntimeError):
    """A git or gh call failed. ``stderr`` holds the tool's output, credentials masked."""

    def __init__(self, message: str, *, stderr: str = "") -> None:
        self.stderr = redact_remote(stderr)
        detail = self.stderr.strip()
        super().__init__(f"{message}: {detail}" if detail else message)


def git_executable() -> str | None:
    return shutil.which("git")


def validate_remote(remote: str) -> str:
    """Return the stripped remote or raise ``ValueError`` with the reason."""
    remote = remote.strip()
    if not remote:
        raise ValueError("remote is empty")
    if remote.startswith("-"):
        raise ValueError("remote may not start with '-'")
    if "::" in remote:
        raise ValueError("remote transport helpers (ext::, …) are not allowed")
    if _URL_RE.match(remote) or _SCP_RE.match(remote) or Path(remote).is_absolute():
        return remote
    raise ValueError(
        "remote must be https://…, ssh://…, file://…, user@host:path, or an absolute path"
    )


def github_owner_repo(remote: str) -> str | None:
    """``owner/repo`` for a github.com remote in any of its usual forms, else ``None``."""
    match = _GITHUB_RE.match(remote.strip())
    if match is None:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _run_gh(gh: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [gh, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=_GH_TIMEOUT_SECONDS,
    )


def github_visibility(owner_repo: str, *, gh: str | None = None) -> str | None:
    """``private`` / ``public`` / ``internal`` per ``gh``; ``None`` when it cannot be determined."""
    gh = gh or shutil.which("gh")
    if gh is None:
        return None
    try:
        proc = _run_gh(
            gh, "repo", "view", owner_repo, "--json", "visibility", "--jq", ".visibility"
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().lower() or None


def github_create_private(owner_repo: str, *, gh: str | None = None) -> None:
    gh = gh or shutil.which("gh")
    if gh is None:
        raise GitError("gh is not installed; create the private repository yourself and re-run")
    try:
        proc = _run_gh(gh, "repo", "create", owner_repo, "--private")
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"could not run gh: {exc}") from exc
    if proc.returncode != 0:
        raise GitError("gh repo create failed", stderr=proc.stderr)


def _subcommand(argv: list[str]) -> str:
    index = 1
    while index < len(argv) and argv[index] in ("-c", "-C"):
        index += 2
    return argv[index] if index < len(argv) else "?"


def _run_git(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        raise GitError(f"could not run git: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(
            f"git {_subcommand(argv)} failed (exit {proc.returncode})", stderr=proc.stderr
        )
    return proc


def _require_git(git: str | None) -> str:
    exe = git or git_executable()
    if exe is None:
        raise GitError("git is not installed (not found on PATH)")
    return exe


class GitRepo:
    """One working clone. Every method shells out to ``git -C <path>`` with the ext guard."""

    def __init__(self, path: Path, *, git: str | None = None) -> None:
        self.path = Path(path)
        self._git = git

    @property
    def git(self) -> str:
        return _require_git(self._git)

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return _run_git([self.git, *_EXT_GUARD, "-C", str(self.path), *args], check=check)

    def exists(self) -> bool:
        return (self.path / ".git").exists()

    @classmethod
    def init(cls, path: Path, remote: str, *, git: str | None = None) -> GitRepo:
        repo = cls(path, git=git)
        repo.path.mkdir(parents=True, exist_ok=True)
        repo.run("init", "-q")
        repo.run("symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}")
        repo.run("remote", "add", "origin", "--", remote)
        repo.configure()
        return repo

    @classmethod
    def clone(cls, remote: str, path: Path, *, git: str | None = None) -> GitRepo:
        repo = cls(path, git=git)
        repo.path.parent.mkdir(parents=True, exist_ok=True)
        _run_git([repo.git, *_EXT_GUARD, "clone", "-q", "--", remote, str(repo.path)])
        repo.configure()
        return repo

    @staticmethod
    def remote_has_commits(remote: str, *, git: str | None = None) -> bool:
        """True when the remote has at least one branch. Raises when it cannot be reached."""
        proc = _run_git([_require_git(git), *_EXT_GUARD, "ls-remote", "--heads", "--", remote])
        return proc.stdout.strip() != ""

    def configure(self) -> None:
        """Repo-local settings the backup relies on; safe to re-run on an adopted clone."""
        self.run("config", "core.autocrlf", "false")
        self.run("config", "commit.gpgsign", "false")
        self._ensure_identity()

    def _ensure_identity(self) -> None:
        if self.run("config", "user.email", check=False).stdout.strip():
            return
        self.run("config", "user.name", IDENTITY_NAME)
        self.run("config", "user.email", IDENTITY_EMAIL)

    def current_branch(self) -> str:
        name = self.run("symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        return name or "HEAD"

    def head(self) -> str | None:
        sha = self.run("rev-parse", "--verify", "-q", "HEAD", check=False).stdout.strip()
        return sha or None

    def fetch_and_ff(self) -> bool:
        """Fetch origin and bring the branch up to date. True when HEAD moved.

        Local commits are rebased on top of the remote; a conflict aborts the
        rebase, leaves the clone as it was, and raises ``GitError``.
        """
        branch = self.current_branch()
        if branch == "HEAD":
            raise GitError("the backup repository is on a detached HEAD")
        self.run("fetch", "-q", "origin")
        upstream = f"refs/remotes/origin/{branch}"
        if self.run("rev-parse", "--verify", "-q", upstream, check=False).returncode != 0:
            return False
        before = self.head()
        if before is None:
            self.run("merge", "-q", "--ff-only", upstream)
        else:
            proc = self.run("rebase", "-q", upstream, check=False)
            if proc.returncode != 0:
                self.run("rebase", "--abort", check=False)
                raise GitError(
                    "could not rebase local backup commits onto the remote", stderr=proc.stderr
                )
        return self.head() != before

    def add_all(self) -> None:
        self.run("add", "-A", "--", ".")

    def has_changes(self) -> bool:
        return self.run("status", "--porcelain").stdout.strip() != ""

    def commit(self, message: str) -> bool:
        """Commit the index. False when nothing is staged."""
        if self.run("diff", "--cached", "--quiet", check=False).returncode == 0:
            return False
        self.run("commit", "-q", "-m", message)
        return True

    def push(self) -> None:
        self.run("push", "-q", "-u", "origin", "HEAD")

    def git_dir_bytes(self) -> int:
        total = 0
        for root, _dirs, files in os.walk(self.path / ".git"):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    continue
        return total
