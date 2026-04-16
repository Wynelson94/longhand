"""Tests for the git operation extractor."""

from longhand.extractors.git import detect_git_command, extract_git_signal


class TestDetectGitCommand:
    def test_simple_git_commit(self):
        assert detect_git_command("git commit -m 'fix bug'") == "commit"

    def test_git_push(self):
        assert detect_git_command("git push origin main") == "push"

    def test_chained_command(self):
        assert detect_git_command("cd /foo && git commit -m 'msg'") == "commit"

    def test_non_git_command(self):
        assert detect_git_command("ls -la") is None

    def test_empty_command(self):
        assert detect_git_command("") is None

    def test_git_status(self):
        assert detect_git_command("git status") == "status"

    def test_git_checkout(self):
        assert detect_git_command("git checkout feature-branch") == "checkout"


class TestExtractCommit:
    def test_standard_commit(self):
        output = (
            "[main abc1234] Fix the bug in parser\n"
            " 2 files changed, 10 insertions(+), 3 deletions(-)\n"
        )
        signal = extract_git_signal("git commit -m 'Fix the bug in parser'", output)
        assert signal is not None
        assert signal.operation_type == "commit"
        assert signal.commit_hash == "abc1234"
        assert signal.commit_message == "Fix the bug in parser"
        assert signal.branch == "main"
        assert signal.files_changed_count == 2
        assert signal.success is True

    def test_commit_with_long_hash(self):
        output = "[feature/auth abc1234567890abcdef1234567890abcdef12345678] Add auth\n 1 file changed"
        signal = extract_git_signal("git commit -m 'Add auth'", output)
        assert signal is not None
        assert signal.commit_hash == "abc1234567890abcdef1234567890abcdef12345678"
        assert signal.branch == "feature/auth"

    def test_commit_amend(self):
        output = "[main def5678] Amended commit message\n 1 file changed"
        signal = extract_git_signal("git commit --amend -m 'Amended commit message'", output)
        assert signal is not None
        assert signal.commit_hash == "def5678"


class TestExtractPush:
    def test_standard_push(self):
        output = (
            "To https://github.com/user/repo.git\n"
            "   abc1234..def5678  main -> main\n"
        )
        signal = extract_git_signal("git push origin main", output)
        assert signal is not None
        assert signal.operation_type == "push"
        assert signal.remote == "https://github.com/user/repo.git"
        assert signal.commit_hash == "def5678"
        assert signal.branch == "main"
        assert signal.success is True

    def test_push_rejected(self):
        output = (
            "To https://github.com/user/repo.git\n"
            " ! [rejected]        main -> main (non-fast-forward)\n"
            "error: failed to push some refs\n"
        )
        signal = extract_git_signal("git push origin main", output)
        assert signal is not None
        assert signal.success is False


class TestExtractCheckout:
    def test_switch_branch(self):
        output = "Switched to branch 'feature-xyz'"
        signal = extract_git_signal("git checkout feature-xyz", output)
        assert signal is not None
        assert signal.operation_type == "checkout"
        assert signal.branch == "feature-xyz"

    def test_new_branch(self):
        output = "Switched to a new branch 'hotfix'"
        signal = extract_git_signal("git checkout -b hotfix", output)
        assert signal is not None
        assert signal.branch == "hotfix"

    def test_already_on_branch(self):
        output = "Already on 'main'"
        signal = extract_git_signal("git checkout main", output)
        assert signal is not None
        assert signal.branch == "main"


class TestExtractMerge:
    def test_successful_merge(self):
        output = "Merge made by the 'ort' strategy.\n 3 files changed"
        signal = extract_git_signal("git merge feature-branch", output)
        assert signal is not None
        assert signal.operation_type == "merge"
        assert signal.branch == "feature-branch"
        assert signal.success is True

    def test_merge_conflict(self):
        output = (
            "Auto-merging src/app.ts\n"
            "CONFLICT (content): Merge conflict in src/app.ts\n"
            "Automatic merge failed; fix conflicts and then commit the result.\n"
        )
        signal = extract_git_signal("git merge feature-branch", output)
        assert signal is not None
        assert signal.success is False


class TestExtractStatus:
    def test_clean_status(self):
        output = "On branch main\nnothing to commit, working tree clean"
        signal = extract_git_signal("git status", output)
        assert signal is not None
        assert signal.operation_type == "status"
        assert signal.branch == "main"

    def test_status_with_changes(self):
        output = (
            "On branch develop\n"
            "Changes not staged for commit:\n"
            "\tmodified:   src/app.ts\n"
            "\tmodified:   src/utils.ts\n"
            "\tdeleted:    src/old.ts\n"
        )
        signal = extract_git_signal("git status", output)
        assert signal is not None
        assert signal.branch == "develop"
        assert signal.files_changed_count == 3


class TestExtractLog:
    def test_full_log(self):
        output = (
            "commit abc1234567890abcdef1234567890abcdef12345678\n"
            "Author: Nate <nate@example.com>\n"
            "Date:   Thu Apr 10 2026\n\n"
            "    Fix parser bug\n\n"
            "commit def5678901234567890abcdef1234567890abcdef12\n"
        )
        signal = extract_git_signal("git log", output)
        assert signal is not None
        assert signal.operation_type == "log"
        assert signal.commit_hash == "abc1234567890abcdef1234567890abcdef12345678"
        assert "2 commits" in (signal.commit_message or "")

    def test_oneline_log(self):
        output = "abc1234 Fix bug\ndef5678 Add feature\n"
        signal = extract_git_signal("git log --oneline", output)
        assert signal is not None
        assert signal.commit_hash == "abc1234"


class TestNonGitCommand:
    def test_ls_returns_none(self):
        assert extract_git_signal("ls -la", "total 42\ndrwxr-xr-x") is None

    def test_npm_returns_none(self):
        assert extract_git_signal("npm install", "added 42 packages") is None

    def test_empty_returns_none(self):
        assert extract_git_signal("", "") is None


class TestGenericGitCommand:
    def test_unknown_subcommand(self):
        signal = extract_git_signal("git stash", "Saved working directory")
        assert signal is not None
        assert signal.operation_type == "stash"
        assert signal.success is True

    def test_git_tag(self):
        signal = extract_git_signal("git tag v1.0.0", "")
        assert signal is not None
        assert signal.operation_type == "tag"
