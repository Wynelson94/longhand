"""Tests for the argv-only git wrapper behind ``longhand backup``.

Every git call is a list-form argv with ``-c protocol.ext.allow=never`` and
``--`` before any remote or path; nothing goes through a shell. The real-git
tests run against a bare repository under ``tmp_path`` and isolate themselves
from the developer's global git config so identity, signing and hook settings
on the machine never leak into the assertions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from longhand.backup import gitrepo
from longhand.backup.gitrepo import (
    GitConflictError,
    GitError,
    GitRepo,
    git_executable,
    github_create_private,
    github_owner_repo,
    github_visibility,
    redact_remote,
    validate_remote,
)

pytestmark = [
    pytest.mark.skipif(git_executable() is None, reason="git is not installed"),
    pytest.mark.usefixtures("isolated_git_config"),
]


def _commit_file(repo: GitRepo, name: str, text: str, message: str) -> None:
    (repo.path / name).write_text(text)
    repo.add_all()
    assert repo.commit(message) is True


# --- remote validation -----------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "-oProxyCommand=x",
        "ext::sh -c 'id'",
        "relative/path",
        "http://x",
        "https://host/with space",
        "git://host/r",
    ],
)
def test_validate_remote_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_remote(bad)


@pytest.mark.parametrize(
    "good",
    [
        "https://github.com/o/r.git",
        "ssh://git@host/r",
        "git@github.com:o/r.git",
        "file:///tmp/r.git",
        "/tmp/r.git",
        "/Volumes/My Drive/longhand.git",
    ],
)
def test_validate_remote_accepts(good: str) -> None:
    assert validate_remote(good) == good


def test_validate_remote_strips_surrounding_whitespace() -> None:
    assert validate_remote("  https://github.com/o/r.git\n") == "https://github.com/o/r.git"


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/o/r.git",
        "https://github.com/o/r",
        "https://github.com/o/r/",
        "git@github.com:o/r.git",
        "git@github.com:o/r",
        "ssh://git@github.com/o/r.git",
        "https://x-access-token:abc123@github.com/o/r.git",
    ],
)
def test_github_owner_repo_parses_github_forms(remote: str) -> None:
    assert github_owner_repo(remote) == "o/r"


@pytest.mark.parametrize(
    "remote",
    ["https://gitlab.com/o/r", "/tmp/r.git", "file:///tmp/r.git", "https://github.com/o"],
)
def test_github_owner_repo_is_none_elsewhere(remote: str) -> None:
    assert github_owner_repo(remote) is None


def test_redact_remote_masks_url_userinfo_only() -> None:
    assert redact_remote("https://user:tok@github.com/o/r.git") == "https://***@github.com/o/r.git"
    assert redact_remote("https://ghp_abc@github.com/o/r.git") == "https://***@github.com/o/r.git"
    assert redact_remote("https://github.com/o/r.git") == "https://github.com/o/r.git"
    assert redact_remote("git@github.com:o/r.git") == "git@github.com:o/r.git"


# --- gh calls (mocked; never run gh in tests) -------------------------------


def test_github_visibility_uses_gh_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "PRIVATE\n", "")

    monkeypatch.setattr(gitrepo.subprocess, "run", fake_run)
    assert github_visibility("o/r", gh="gh") == "private"
    argv, kwargs = calls[0]
    assert argv[:4] == ["gh", "repo", "view", "o/r"]
    assert "--jq" in argv
    assert "shell" not in kwargs


def test_github_visibility_is_none_when_gh_is_missing_or_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gitrepo.shutil, "which", lambda name: None)
    assert github_visibility("o/r") is None

    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "not found")

    monkeypatch.setattr(gitrepo.subprocess, "run", failing_run)
    assert github_visibility("o/r", gh="gh") is None


def test_github_create_private_argv_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gitrepo.subprocess, "run", fake_run)
    github_create_private("o/r", gh="gh")
    assert calls == [["gh", "repo", "create", "o/r", "--private"]]

    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    monkeypatch.setattr(gitrepo.subprocess, "run", failing_run)
    with pytest.raises(GitError) as info:
        github_create_private("o/r", gh="gh")
    assert info.value.stderr == "boom"

    monkeypatch.setattr(gitrepo.shutil, "which", lambda name: None)
    with pytest.raises(GitError):
        github_create_private("o/r")


def test_git_error_masks_url_credentials() -> None:
    err = GitError(
        "git push failed", stderr="fatal: unable to access 'https://u:tok@github.com/o/r.git/'"
    )
    assert "tok" not in str(err)
    assert "tok" not in err.stderr
    assert "***@github.com" in err.stderr


# --- real git against a bare remote ----------------------------------------


def test_run_never_uses_shell_and_adds_ext_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(gitrepo.subprocess, "run", fake_run)
    GitRepo(tmp_path, git="git").run("status", "--porcelain")
    argv, kwargs = calls[0]
    assert argv[:5] == ["git", "-c", "protocol.ext.allow=never", "-C", str(tmp_path)]
    assert argv[5:] == ["status", "--porcelain"]
    assert "shell" not in kwargs
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True


def test_run_raises_git_error_with_stderr(tmp_path: Path, bare_remote: Path) -> None:
    repo = GitRepo.init(tmp_path / "r", str(bare_remote))
    with pytest.raises(GitError) as info:
        repo.run("rev-parse", "--verify", "definitely-not-a-ref")
    assert info.value.stderr.strip() != ""
    assert repo.run("rev-parse", "--verify", "nope", check=False).returncode != 0


def test_init_creates_main_with_remote_and_identity(tmp_path: Path, bare_remote: Path) -> None:
    repo = GitRepo.init(tmp_path / "r", str(bare_remote))
    assert repo.exists()
    assert repo.current_branch() == "main"
    assert repo.run("remote", "get-url", "origin").stdout.strip() == str(bare_remote)
    assert repo.run("config", "core.autocrlf").stdout.strip() == "false"
    assert repo.run("config", "user.email").stdout.strip() == "backup@longhand.invalid"
    assert repo.head() is None
    assert repo.has_changes() is False


def test_init_keeps_an_existing_identity(
    tmp_path: Path, bare_remote: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    repo = GitRepo.init(tmp_path / "r", str(bare_remote))
    assert repo.run("config", "user.email").stdout.strip() == "me@example.com"


def test_commit_push_clone_round_trip(tmp_path: Path, bare_remote: Path) -> None:
    repo = GitRepo.init(tmp_path / "a", str(bare_remote))
    assert GitRepo.remote_has_commits(str(bare_remote)) is False

    (repo.path / "hello.txt").write_text("hi\n")
    assert repo.has_changes() is True
    repo.add_all()
    assert repo.commit("longhand backup: 1 added") is True
    assert repo.head() is not None
    assert repo.has_changes() is False
    assert repo.commit("nothing here") is False

    repo.push()
    assert GitRepo.remote_has_commits(str(bare_remote)) is True

    clone = GitRepo.clone(str(bare_remote), tmp_path / "b")
    assert clone.exists()
    assert (clone.path / "hello.txt").read_text() == "hi\n"
    assert clone.current_branch() == "main"
    assert clone.head() == repo.head()
    assert clone.run("config", "core.autocrlf").stdout.strip() == "false"
    assert clone.run("config", "user.email").stdout.strip() == "backup@longhand.invalid"
    assert clone.git_dir_bytes() > 0


def test_remote_has_commits_raises_when_unreachable(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        GitRepo.remote_has_commits(str(tmp_path / "missing.git"))


def test_fetch_and_ff_pulls_other_clone(tmp_path: Path, bare_remote: Path) -> None:
    a = GitRepo.init(tmp_path / "a", str(bare_remote))
    _commit_file(a, "f1.txt", "one\n", "first")
    a.push()
    b = GitRepo.clone(str(bare_remote), tmp_path / "b")
    assert b.fetch_and_ff() is False

    _commit_file(a, "f2.txt", "two\n", "second")
    a.push()
    assert b.fetch_and_ff() is True
    assert (b.path / "f2.txt").read_text() == "two\n"
    assert b.head() == a.head()


def test_fetch_and_ff_rebases_local_commits_on_top(tmp_path: Path, bare_remote: Path) -> None:
    a = GitRepo.init(tmp_path / "a", str(bare_remote))
    _commit_file(a, "f1.txt", "one\n", "first")
    a.push()
    b = GitRepo.clone(str(bare_remote), tmp_path / "b")

    _commit_file(a, "f2.txt", "two\n", "second")
    a.push()
    _commit_file(b, "f3.txt", "three\n", "local only")

    assert b.fetch_and_ff() is True
    assert (b.path / "f2.txt").exists() and (b.path / "f3.txt").exists()
    assert b.run("log", "-1", "--format=%s").stdout.strip() == "local only"
    b.push()
    assert a.fetch_and_ff() is True
    assert (a.path / "f3.txt").exists()


def test_fetch_and_ff_on_unborn_branch_takes_the_remote(tmp_path: Path, bare_remote: Path) -> None:
    a = GitRepo.init(tmp_path / "a", str(bare_remote))
    _commit_file(a, "f1.txt", "one\n", "first")
    a.push()
    b = GitRepo.init(tmp_path / "b", str(bare_remote))
    assert b.head() is None
    assert b.fetch_and_ff() is True
    assert (b.path / "f1.txt").read_text() == "one\n"
    assert b.head() == a.head()


def test_fetch_and_ff_conflict_aborts_and_raises(tmp_path: Path, bare_remote: Path) -> None:
    a = GitRepo.init(tmp_path / "a", str(bare_remote))
    _commit_file(a, "f.txt", "a\n", "first")
    a.push()
    b = GitRepo.clone(str(bare_remote), tmp_path / "b")
    _commit_file(a, "f.txt", "a2\n", "theirs")
    a.push()
    _commit_file(b, "f.txt", "b2\n", "ours")

    with pytest.raises(GitConflictError) as info:
        b.fetch_and_ff()
    assert isinstance(info.value, GitError)
    assert not (b.path / ".git" / "rebase-merge").exists()
    assert not (b.path / ".git" / "rebase-apply").exists()
    assert (b.path / "f.txt").read_text() == "b2\n"
    assert b.has_changes() is False


def test_unpushed_commits_counts_local_commits(tmp_path: Path, bare_remote: Path) -> None:
    a = GitRepo.init(tmp_path / "a", str(bare_remote))
    assert a.unpushed_commits() == 0
    _commit_file(a, "f1.txt", "one\n", "first")
    assert a.unpushed_commits() == 1
    a.push()
    assert a.unpushed_commits() == 0
    _commit_file(a, "f2.txt", "two\n", "second")
    assert a.unpushed_commits() == 1
    b = GitRepo.clone(str(bare_remote), tmp_path / "b")
    assert b.unpushed_commits() == 0
