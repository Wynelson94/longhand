"""``longhand backup`` — opt-in, git-backed backup of the whole archive.

Nothing leaves the machine until ``backup init`` has shown what would be
uploaded and the user has typed ``yes``. The run itself lives in
``longhand.backup.service``; this module is the terminal in front of it.
See ``docs/backup.md`` for what is uploaded and how to restore.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from longhand.cli.helpers import _get_store, console

backup_app = typer.Typer(
    name="backup",
    help=(
        "Back the archive up to a git remote you own "
        "(opt-in: nothing leaves this machine until you enable it)"
    ),
    invoke_without_command=True,
)

INIT_HINT = "longhand backup init --remote <url>"
GIT_COMPRESSION_RATIO = 0.37  # measured on a 20k-line sample of real transcripts


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _human(n: int) -> str:
    from longhand.setup_commands import _human_size

    return _human_size(n)


def _mask_summary(counts: dict[str, int], sessions: int) -> str:
    if not counts:
        return "none found"
    parts = ", ".join(f"{name}: {n}" for name, n in sorted(counts.items()))
    return f"{parts} ({sessions} session{'s' if sessions != 1 else ''})"


def _not_enabled() -> None:
    console.print(f"[yellow]Backups are not enabled. Run: {INIT_HINT}[/yellow]")


# ─── backup (run) ─────────────────────────────────────────────────────────────


@backup_app.callback(invoke_without_command=True)
def backup_root(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Plan only: what would be exported, how big, what gets masked"
    ),
    no_push: bool = typer.Option(False, "--no-push", help="Commit into the clone, do not push"),
    no_mask: bool = typer.Option(
        False,
        "--no-mask",
        help="Export verbatim: secret-shaped strings are NOT masked (asks you to type 'verbatim')",
    ),
    data_dir: str | None = typer.Option(None, "--data-dir"),
) -> None:
    """Back up every changed session to the remote you enabled with init.

    Plain `longhand backup` runs now. Secret-shaped strings are masked in the
    uploaded copy; the local store is never changed. Sub-commands: init
    (enable, with the consent screen), status, auto on|off, disable.
    """
    if ctx.invoked_subcommand is not None:
        return
    _run(dry_run=dry_run, push=not no_push, mask=not no_mask, data_dir=data_dir)


def _run(*, dry_run: bool, push: bool, mask: bool, data_dir: str | None) -> None:
    from longhand.backup.service import BackupDisabledError, run_backup

    if not mask:
        if not _stdin_is_tty():
            console.print(
                "[red]✗ --no-mask uploads secret-shaped strings verbatim and needs a terminal "
                "to confirm on: run it interactively and type verbatim at the prompt.[/red]"
            )
            raise typer.Exit(1)
        console.print(
            "[yellow]--no-mask: API keys, tokens, private keys and card numbers in the "
            "archive will be uploaded verbatim.[/yellow]"
        )
        if typer.prompt("Type verbatim to continue").strip() != "verbatim":
            console.print("[yellow]Aborted — nothing changed, nothing uploaded.[/yellow]")
            raise typer.Exit(1)

    store = _get_store(data_dir)
    try:
        with console.status("Backing up…") as status:
            report = run_backup(
                store.sqlite,
                store.data_dir,
                dry_run=dry_run,
                push=push,
                mask=mask,
                progress=lambda line: status.update(f"Backing up… {escape(line)}"),
            )
    except BackupDisabledError as exc:
        _not_enabled()
        console.print(f"[dim]{escape(str(exc))}[/dim]")
        raise typer.Exit(1) from None

    masks = _mask_summary(report.mask_counts, report.sessions_masked)
    if report.dry_run:
        console.print(
            f"Dry run: {report.planned} to add or update, "
            f"{_human(report.bytes_exported)} of transcript text; nothing was written."
        )
        console.print(f"  masked in the copy: {masks}")
        return

    current = report.unchanged + report.subset
    console.print(
        f"[green]✓[/green] {report.added} added, {report.grown} updated, "
        f"{current} already current ({report.planned} checked)"
    )
    pushed = "pushed" if report.pushed else "not pushed"
    console.print(
        f"  {_human(report.bytes_exported)} exported, {report.commits} commit(s), {pushed}"
    )
    console.print(f"  masked in the copy: {masks}")
    if report.error:
        console.print(f"[red]✗ {escape(report.error)}[/red]")
        raise typer.Exit(1)


# ─── backup init ──────────────────────────────────────────────────────────────


def _consent_text(remote: str, repo_path: Path, estimate: Any, redact_on: bool) -> str:
    packed = int(estimate.bytes * GIT_COMPRESSION_RATIO)
    secrets = _mask_summary(estimate.mask_counts, estimate.sessions_with_masks)
    return "\n".join(
        [
            "Longhand backup — what leaves this machine",
            "",
            f"  Remote:        {remote}",
            f"  Repo clone:    {repo_path}",
            f"  Sessions:      {estimate.sessions} "
            f"({estimate.claude} Claude Code, {estimate.codex} Codex)",
            f"  Upload size:   {_human(estimate.bytes)} of transcript text "
            f"(about {_human(packed)} once git compresses it)",
            "  Content:       verbatim transcripts — prompts, thinking, tool output, file contents",
            "  Encryption:    none (whoever hosts the remote can read it)",
            f"  Secrets:       secret-shaped strings are masked in the copy — {secrets}",
            f"  Local store:   unchanged (redact.enabled = {redact_on})",
            "",
            "Nothing is uploaded until you type yes.",
        ]
    )


@backup_app.command("init")
def backup_init(
    remote: str = typer.Option(
        ...,
        "--remote",
        help="A git remote you own: https://…, ssh://…, git@host:path, or a local path",
    ),
    path: str | None = typer.Option(
        None, "--path", help="Where to keep the clone (default: <data dir>/backup)"
    ),
    create: bool = typer.Option(
        False, "--create", help="Create the GitHub repository first, private (needs gh)"
    ),
    yes: bool = typer.Option(
        False, "--yes", help="Accept the consent text without a prompt (read it first)"
    ),
    data_dir: str | None = typer.Option(None, "--data-dir"),
) -> None:
    """Enable backups: check the remote is private, show what would leave, ask.

    Nothing is uploaded here. The first upload is `longhand backup`.
    """
    from longhand.backup.gitrepo import (
        GitError,
        GitRepo,
        git_executable,
        github_create_private,
        github_owner_repo,
        github_visibility,
        redact_remote,
        validate_remote,
    )
    from longhand.backup.service import BackupConfig, estimate_export
    from longhand.redaction import redaction_enabled

    if git_executable() is None:
        console.print(
            "[red]✗ git is not installed (not found on PATH). Install git and re-run.[/red]"
        )
        raise typer.Exit(1)
    try:
        remote = validate_remote(remote)
    except ValueError as exc:
        shown = escape(redact_remote(remote.strip()))
        console.print(f"[red]✗ {shown!r} is not a supported remote: {exc}[/red]")
        raise typer.Exit(1) from None
    shown_remote = redact_remote(remote)

    owner_repo = github_owner_repo(remote)
    if create:
        if owner_repo is None:
            console.print(
                "[red]✗ --create needs a github.com remote "
                "(https://github.com/<owner>/<repo>).[/red]"
            )
            raise typer.Exit(1)
        try:
            github_create_private(owner_repo)
        except GitError as exc:
            console.print(f"[red]✗ {escape(str(exc))}[/red]")
            raise typer.Exit(1) from None
        console.print(f"[green]✓[/green] Created {owner_repo} as a private repository")

    attest = True
    if owner_repo is not None:
        visibility = github_visibility(owner_repo)
        if visibility == "private":
            attest = False
            console.print(f"[green]✓[/green] {owner_repo} is private on GitHub")
        elif visibility in ("public", "internal"):
            console.print(
                f"[red]✗ {owner_repo} is {visibility.upper()} on GitHub. Longhand will not back "
                "up to a repository other people can read: make it private, or use another "
                "remote.[/red]"
            )
            raise typer.Exit(1)
        else:
            console.print(
                "[yellow]Could not confirm on GitHub that this repository is private "
                "(gh is not installed or not signed in). You are responsible for that.[/yellow]"
            )
    else:
        console.print(
            "[yellow]Not a github.com remote, so Longhand cannot check that it is private. "
            "You are responsible for that.[/yellow]"
        )

    store = _get_store(data_dir)
    repo_path = Path(path).expanduser() if path else store.data_dir / "backup"
    with console.status("Measuring the archive…") as status:
        estimate = estimate_export(
            store.sqlite,
            progress=lambda i, n: status.update(f"Measuring the archive… {i}/{n}"),
        )
    console.print()
    console.print(
        _consent_text(shown_remote, repo_path, estimate, redaction_enabled()),
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    console.print()

    if not yes:
        if not _stdin_is_tty():
            console.print(
                "[red]✗ No terminal to confirm on. Re-run with --yes after reading the "
                "consent text above.[/red]"
            )
            raise typer.Exit(1)
        if typer.prompt("Type yes to enable backups").strip().lower() != "yes":
            console.print("[yellow]Aborted — nothing changed, nothing uploaded.[/yellow]")
            raise typer.Exit(1)
        if (
            attest
            and typer.prompt("Type private to confirm this remote is not public").strip().lower()
            != "private"
        ):
            console.print("[yellow]Aborted — nothing changed, nothing uploaded.[/yellow]")
            raise typer.Exit(1)

    try:
        if (repo_path / ".git").exists():
            repo = GitRepo(repo_path)
            repo.configure()
            current = repo.run("remote", "get-url", "origin", check=False).stdout.strip()
            if current != remote:
                repo.run("remote", "set-url", "origin", "--", remote)
                console.print(
                    f"[dim]Using the existing clone at {repo_path}; origin now points at the remote above[/dim]"
                )
            else:
                console.print(f"[dim]Using the existing clone at {repo_path}[/dim]")
        elif GitRepo.remote_has_commits(remote):
            with console.status("Cloning the remote…"):
                repo = GitRepo.clone(remote, repo_path)
            console.print(f"[dim]Cloned the remote into {repo_path}[/dim]")
        else:
            repo = GitRepo.init(repo_path, remote)
            console.print(f"[dim]Created an empty clone at {repo_path}[/dim]")
    except GitError as exc:
        console.print(f"[red]✗ {escape(str(exc))}[/red]")
        raise typer.Exit(1) from None

    BackupConfig(enabled=True, remote=remote, path=str(repo_path), auto=False).save(store.data_dir)
    console.print("[green]✓[/green] Backups enabled. Nothing has been uploaded yet.")
    console.print("  Plan first:     longhand backup --dry-run")
    console.print("  First upload:   longhand backup")
    console.print("  Hourly, automatic, from the scheduled reconciler:   longhand backup auto on")


# ─── backup status / auto / disable ───────────────────────────────────────────


@backup_app.command("status")
def backup_status_cmd(data_dir: str | None = typer.Option(None, "--data-dir")) -> None:
    """Where the backup stands: remote, clone, pending sessions, last push, masks."""
    from longhand.backup.service import backup_status

    store = _get_store(data_dir)
    status = backup_status(store.data_dir, store.sqlite)
    masked = status["last_mask_counts"]
    masked_text = (
        ", ".join(f"{name}: {n}" for name, n in sorted(masked.items())) if masked else "none"
    )
    lines = [
        f"  Backups:            {'on' if status['enabled'] else 'off'}",
        f"  Auto (reconciler):  {'on' if status['auto'] else 'off'}",
        f"  Remote:             {status['remote'] or '—'}",
        f"  Clone:              {status['path']}{'' if status['repo_exists'] else '  (missing)'}",
        f"  Sessions backed up: {status['sessions_backed_up']}",
        f"  Pending (changed):  {status['pending']}",
        f"  Unpushed commits:   {status['unpushed_commits']}",
        f"  Last run:           {status['last_run_at'] or 'never'}",
        f"  Last push:          {status['last_push_at'] or 'never'}",
        f"  Last error:         {status['last_error'] or 'none'}",
        f"  Clone size (.git):  {_human(status['repo_bytes'])}",
        f"  Masked last run:    {masked_text}",
    ]
    console.print("Longhand backup")
    console.print("\n".join(lines), markup=False, highlight=False, soft_wrap=True)
    if not status["enabled"]:
        console.print(f"[dim]Enable with: {INIT_HINT}[/dim]")


@backup_app.command("auto")
def backup_auto(
    mode: str = typer.Argument(..., help="on or off"),
    data_dir: str | None = typer.Option(None, "--data-dir"),
) -> None:
    """Let the scheduled reconciler push, at most hourly, only when something changed."""
    from longhand.backup.service import BackupConfig
    from longhand.storage.store import resolve_data_dir

    if mode not in ("on", "off"):
        console.print("[red]✗ Use: longhand backup auto on|off[/red]")
        raise typer.Exit(1)
    resolved = resolve_data_dir(data_dir)
    config = BackupConfig.load(resolved)
    if not config.enabled:
        _not_enabled()
        raise typer.Exit(1)
    config.auto = mode == "on"
    config.save(resolved)
    if config.auto:
        console.print(
            "[green]✓[/green] Automatic backups on: the scheduled reconciler pushes at most "
            "once an hour, only when something changed, masking always on."
        )
        console.print("[dim]Needs the reconciler: longhand schedule install-reconciler[/dim]")
    else:
        console.print("[green]✓[/green] Automatic backups off. Manual runs: longhand backup")


@backup_app.command("disable")
def backup_disable(data_dir: str | None = typer.Option(None, "--data-dir")) -> None:
    """Turn backups off. The clone and the remote keep what was already pushed."""
    from longhand.backup.service import BackupConfig
    from longhand.storage.store import resolve_data_dir

    resolved = resolve_data_dir(data_dir)
    config = BackupConfig.load(resolved)
    config.enabled = False
    config.auto = False
    config.save(resolved)
    console.print(
        f"[green]✓[/green] Backups disabled. The clone stays at {config.repo_path(resolved)}; "
        "delete it yourself if you want it gone. The remote keeps what was pushed."
    )
