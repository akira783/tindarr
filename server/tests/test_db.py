"""Transactions: SQLAlchemy, not the sqlite3 driver, decides when they begin."""

import asyncio
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.storage.db import (
    create_sync_engine,
    database_path,
    sync_write_transaction,
    write_transaction,
)
from tindarr.storage.tables import settings

pytestmark = pytest.mark.anyio


def table_names(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return {row[0] for row in rows}


def test_ddl_is_rolled_back_with_its_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    engine = create_sync_engine(db_path)

    def create_then_fail() -> None:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE a (x INTEGER)")
            connection.exec_driver_sql("CREATE TABLE b (x INTEGER)")
            raise RuntimeError

    try:
        with pytest.raises(RuntimeError):
            create_then_fail()
    finally:
        engine.dispose()
    assert table_names(db_path) == set()


def test_write_transaction_holds_the_write_lock_from_the_start(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    engine = create_sync_engine(db_path)
    try:
        with (
            sync_write_transaction(engine),
            closing(sqlite3.connect(db_path, timeout=0, isolation_level=None)) as other,
            # No statement ran yet, but BEGIN IMMEDIATE already took the lock.
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            other.execute("BEGIN IMMEDIATE")
    finally:
        engine.dispose()


def test_reads_see_one_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    with closing(sqlite3.connect(db_path)) as setup, setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE t (x INTEGER)")
        setup.execute("INSERT INTO t VALUES (1)")
    engine = create_sync_engine(db_path)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM t")) == 1
            with closing(sqlite3.connect(db_path)) as writer, writer:
                writer.execute("INSERT INTO t VALUES (2)")
            assert connection.scalar(text("SELECT count(*) FROM t")) == 1
    finally:
        engine.dispose()


async def test_read_then_write_is_atomic(engine: AsyncEngine, data_dir: Path) -> None:
    async with write_transaction(engine) as connection:
        await connection.execute(
            settings.insert().values(
                name="counter", value="0", encrypted=False, updated_at=datetime.now(UTC)
            )
        )

    async def increment() -> None:
        async with write_transaction(engine) as connection:
            value = await connection.scalar(
                select(settings.c.value).where(settings.c.name == "counter")
            )
            await asyncio.sleep(0.02)  # let the other tasks try to interleave
            await connection.execute(
                update(settings)
                .where(settings.c.name == "counter")
                .values(value=str(int(str(value)) + 1))
            )

    await asyncio.gather(*(increment() for _ in range(5)))
    with closing(sqlite3.connect(database_path(data_dir))) as connection:
        assert connection.execute("SELECT value FROM settings WHERE name='counter'").fetchall() == [
            ("5",)
        ]


async def test_write_transaction_rolls_back_on_error(engine: AsyncEngine) -> None:
    async def insert_then_fail() -> None:
        async with write_transaction(engine) as connection:
            await connection.execute(
                settings.insert().values(
                    name="ghost", value="1", encrypted=False, updated_at=datetime.now(UTC)
                )
            )
            raise RuntimeError

    with pytest.raises(RuntimeError):
        await insert_then_fail()
    async with engine.connect() as connection:
        assert await connection.scalar(select(settings.c.name)) is None
