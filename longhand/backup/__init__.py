"""Opt-in git-backed backup and restore of the Longhand archive.

Nothing in this package runs unless the user has enabled backups with
the ``backup init`` command. See ``docs/backup.md`` for what leaves the machine.
"""

from longhand.backup.service import (
    BackupConfig,
    BackupDisabledError,
    RunReport,
    backup_status,
    backup_status_line,
    maybe_auto_backup,
    run_backup,
)
from longhand.backup.state import BackupState

__all__ = [
    "BackupConfig",
    "BackupDisabledError",
    "BackupState",
    "RunReport",
    "backup_status",
    "backup_status_line",
    "maybe_auto_backup",
    "run_backup",
]
