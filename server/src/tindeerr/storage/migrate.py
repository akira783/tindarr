"""Schema migrations at startup, with a consistent backup before any upgrade.

When the database already exists and its revision differs from the code's head, a copy
is taken first through SQLite's online backup API (consistent even with WAL, unlike a
file copy) into ``<data_dir>/backups/``, with the old revision in the file name. Only
the most recent backups are kept.
"""

import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from tindeerr.storage.db import create_sync_engine

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"
BACKUP_PREFIX: Final = "tindeerr-"
BACKUP_SUFFIX: Final = ".db"

logger = logging.getLogger(__name__)


class SchemaTooNewError(RuntimeError):
    """The database was migrated by a newer Tindeerr version than this one."""


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


def upgrade_database(db_path: Path, backups_dir: Path, keep: int) -> MigrationResult:
    """Bring the database at ``db_path`` to the head revision (blocking).

    Creates the database if needed. Refuses to touch a database whose revision this
    version does not know, which happens after a downgrade of the server.
    """
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
                    "it was probably migrated by a newer Tindeerr. Upgrade the server, or "
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
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        logger.info("database migrated", extra={"from_revision": current, "to_revision": head})
        return MigrationResult(current, head, backup)
    finally:
        engine.dispose()


def backup_database(db_path: Path, backups_dir: Path, revision: str) -> Path:
    """Copy the database with SQLite's backup API into a new file, mode 0600."""
    backups_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = backups_dir / f"{BACKUP_PREFIX}{stamp}-rev-{revision}{BACKUP_SUFFIX}"
    # Create the file first so it never exists with looser permissions.
    os.close(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    with (
        closing(sqlite3.connect(db_path)) as source,
        closing(sqlite3.connect(target)) as destination,
    ):
        source.backup(destination)
    return target


def prune_backups(backups_dir: Path, keep: int) -> list[Path]:
    """Delete all but the ``keep`` most recent backups; return the deleted paths."""
    # Names start with a UTC timestamp, so lexical order is chronological order.
    backups = sorted(backups_dir.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))
    stale = backups[: max(len(backups) - keep, 0)]
    for path in stale:
        path.unlink()
    return stale
