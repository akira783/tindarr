"""Engine creation, SQLite connection settings and transactions.

Python's sqlite3 driver (and aiosqlite on top of it) manages transactions itself in a
legacy way: it never sends ``BEGIN`` before a ``SELECT`` or DDL, so a read-then-write
sequence is not atomic and a failed migration leaves half its tables behind. Both
engines therefore use SQLAlchemy's documented recipe: the driver's own handling is
switched off (``isolation_level = None``) and SQLAlchemy emits ``BEGIN`` itself when a
transaction starts. Then:

- ``engine.connect()`` reads run in a ``BEGIN DEFERRED`` transaction (one consistent
  snapshot, rolled back at the end of the block);
- ``write_transaction`` (and ``sync_write_transaction``) take SQLite's write lock up
  front with ``BEGIN IMMEDIATE``, so a read followed by a write in the same block (token
  rotation, one-time codes) cannot interleave with another writer. Concurrent writers
  wait for each other, up to ``BUSY_TIMEOUT_MS``;
- DDL is transactional: a migration either fully applies or leaves nothing behind.
"""

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast, override

from sqlalchemy import Connection, DateTime, Engine, create_engine, event
from sqlalchemy.engine.interfaces import DBAPIConnection, Dialect
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.types import TypeDecorator

DB_FILE_NAME: Final = "tindeerr.db"
BUSY_TIMEOUT_MS: Final = 5000
#: Execution option marking an engine whose transactions start with BEGIN IMMEDIATE.
WRITE_OPTION: Final = "tindeerr_write"

_PRAGMAS: Final = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}",
)


def database_path(data_dir: Path) -> Path:
    """Return the path of the SQLite database inside the data directory."""
    return data_dir / DB_FILE_NAME


def _on_connect(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
    # The driver must not open or commit transactions behind SQLAlchemy's back.
    cast("Any", dbapi_connection).isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
    finally:
        cursor.close()


def _on_begin(connection: Connection) -> None:
    write = connection.get_execution_options().get(WRITE_OPTION, False)
    connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN DEFERRED")


def _configure(engine: Engine) -> None:
    event.listen(engine, "connect", _on_connect)
    event.listen(engine, "begin", _on_begin)


def create_sync_engine(path: Path) -> Engine:
    """Blocking engine, used for migrations and backups before the app starts serving."""
    engine = create_engine(f"sqlite:///{path}")
    _configure(engine)
    return engine


def create_async_db_engine(path: Path) -> AsyncEngine:
    """Async engine (aiosqlite) used while serving requests."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    _configure(engine.sync_engine)
    return engine


@asynccontextmanager
async def write_transaction(engine: AsyncEngine) -> AsyncGenerator[AsyncConnection]:
    """Run the block in a transaction holding the database's write lock from the start.

    Use it for every write, and for any read whose result decides a write (check a
    one-time code, then mark it used): no other writer can commit in between. Commits
    when the block ends, rolls back if it raises.
    """
    async with engine.execution_options(**{WRITE_OPTION: True}).begin() as connection:
        yield connection


@contextmanager
def sync_write_transaction(engine: Engine) -> Generator[Connection]:
    """Blocking twin of ``write_transaction``, for migrations."""
    with engine.execution_options(**{WRITE_OPTION: True}).begin() as connection:
        yield connection


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
