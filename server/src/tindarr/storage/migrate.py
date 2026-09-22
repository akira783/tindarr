"""Schema migrations at startup, with a consistent backup before any upgrade.

When the database already exists and its revision differs from the code's head, a copy
is taken first through SQLite's online backup API (consistent even with WAL, unlike a
file copy) into ``<data_dir>/backups/``, with the old revision in the file name. The
copy is checked (``PRAGMA integrity_check``) and flushed to disk before the upgrade
starts. Only the most recent backups are kept.

The whole upgrade runs in one transaction (see ``tindarr.storage.db``): a failing
revision leaves the database as it was. An exclusive lock on ``<data_dir>/.migrate.lock``
serialises processes started at the same time on the same data directory.
"""

import fcntl
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from tindarr.storage.db import create_sync_engine, sync_write_transaction

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"
BACKUP_PREFIX: Final = "tindarr-"
BACKUP_SUFFIX: Final = ".db"
LOCK_FILE_NAME: Final = ".migrate.lock"

logger = logging.getLogger(__name__)


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer Tindarr version than this one."""


class BackupError(RuntimeError):
    """The pre-migration backup could not be taken or failed its integrity check."""


@dataclass(frozen=True)
class MigrationResult:
    """What ``upgrade_database`` did."""

    from_revision: str | None
    to_revision: str
    backup: Path | None

    @property
    def upgraded(self) -> bool:
        """Whether a migration ran."""
        return self.from_revision != self.to_revision


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def head_revision() -> str:
    """Return the revision the code expects."""
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if head is None:  # pragma: no cover - there is always at least one revision
        msg = "no migration found"
        raise RuntimeError(msg)
    return head


@contextmanager
def migration_lock(directory: Path) -> Generator[None]:
    """Hold an exclusive lock on ``<directory>/.migrate.lock`` (blocking until free)."""
    fd = os.open(
        directory / LOCK_FILE_NAME,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


def upgrade_database(db_path: Path, backups_dir: Path, keep: int) -> MigrationResult:
    """Bring the database at ``db_path`` to the head revision (blocking).

    Creates the database if needed. Refuses to touch a database whose revision this
    version does not know, which happens after a downgrade of the server.
    """
    with migration_lock(db_path.parent):
        return _upgrade(db_path, backups_dir, keep)


def _upgrade(db_path: Path, backups_dir: Path, keep: int) -> MigrationResult:
    script = ScriptDirectory.from_config(_alembic_config())
    head = head_revision()
    if not db_path.exists():
        # SQLite gives the -wal and -shm files the database file's permissions.
        os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    engine = create_sync_engine(db_path)
    try:
        with engine.connect() as connection:
            current = MigrationContext.configure(connection).get_current_revision()
        if current == head:
            return MigrationResult(current, head, None)

        backup = None
        if current is not None:
            try:
                script.get_revision(current)
            except CommandError:
                msg = (
                    f"database revision {current} is unknown to this version (head {head}); "
                    "it was probably migrated by a newer Tindarr. Upgrade the server, or "
                    "restore a backup from before that upgrade."
                )
                raise SchemaTooNewError(msg) from None
            backup = backup_database(db_path, backups_dir, current)
            prune_backups(backups_dir, keep)
            logger.info(
                "database backed up before migration",
                extra={"backup": str(backup), "from_revision": current},
            )

        config = _alembic_config()
        # One transaction for every pending revision: all of them apply, or none.
        with sync_write_transaction(engine) as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        logger.info("database migrated", extra={"from_revision": current, "to_revision": head})
        return MigrationResult(current, head, backup)
    finally:
        engine.dispose()


def backup_database(db_path: Path, backups_dir: Path, revision: str) -> Path:
    """Copy the database with SQLite's backup API into a new file, mode 0600.

    The copy is verified and flushed to disk; a copy that fails is deleted and
    ``BackupError`` is raised, so no migration runs without a usable backup.
    """
    backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backups_dir / f"{BACKUP_PREFIX}{stamp}-rev-{revision}{BACKUP_SUFFIX}"
    # Create the file first so it never exists with looser permissions.
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        with (
            closing(sqlite3.connect(db_path)) as source,
            closing(sqlite3.connect(target)) as destination,
        ):
            source.backup(destination)
            # A self-contained file: no -wal or -shm companion to copy or lose.
            destination.execute("PRAGMA journal_mode=DELETE")
        verify_backup(target)
        _fsync(target)
        _fsync(backups_dir)
    except (sqlite3.Error, OSError, BackupError) as exc:
        for leftover in (target, *(Path(f"{target}{suffix}") for suffix in ("-wal", "-shm"))):
            leftover.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        msg = f"cannot back up the database before migrating it: {exc}"
        raise BackupError(msg) from exc
    return target


def verify_backup(path: Path) -> None:
    """Raise ``BackupError`` unless ``PRAGMA integrity_check`` passes on ``path``."""
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        msg = f"backup {path.name} is unreadable: {exc}"
        raise BackupError(msg) from None
    if rows != [("ok",)]:
        msg = f"backup {path.name} failed its integrity check"
        raise BackupError(msg)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    """Delete all but the ``keep`` most recent backups; return the deleted paths."""
    # Names start with a UTC timestamp, so lexical order is chronological order.
    backups = sorted(backups_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))
    stale = backups[: max(len(backups) - keep, 0)]
    for path in stale:
        path.unlink()
    return stale
