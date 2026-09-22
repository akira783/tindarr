"""Engine creation and SQLite connection settings."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Final, override

from sqlalchemy import DateTime, Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection, Dialect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.types import TypeDecorator

DB_FILE_NAME: Final = "tindeerr.db"
BUSY_TIMEOUT_MS: Final = 5000

_PRAGMAS: Final = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
)


def database_path(data_dir: Path) -> Path:
    """Return the path of the SQLite database inside the data directory."""
    return data_dir / DB_FILE_NAME


def _apply_pragmas(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def create_sync_engine(path: Path) -> Engine:
    """Blocking engine, used for migrations and backups before the app starts serving."""
    engine = create_engine(f"sqlite:///{path}")
    event.listen(engine, "connect", _apply_pragmas)
    return engine


def create_async_db_engine(path: Path) -> AsyncEngine:
    """Async engine (aiosqlite) used while serving requests."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    event.listen(engine.sync_engine, "connect", _apply_pragmas)
    return engine


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime, stored as a naive UTC timestamp.

    SQLite has no timezone support; this keeps every stored time in UTC and every
    loaded time aware, so naive datetimes never leak into the code.
    """

    impl = DateTime
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "naive datetime; use an aware UTC datetime"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    @override
    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)
