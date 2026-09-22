import fcntl
import os
import shutil
import sqlite3
import stat
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from tindeerr.storage import migrate
from tindeerr.storage.db import UtcDateTime, create_sync_engine, database_path
from tindeerr.storage.migrate import (
    LOCK_FILE_NAME,
    BackupError,
    MigrationResult,
    SchemaTooNewError,
    head_revision,
    prune_backups,
    upgrade_database,
    verify_backup,
)
from tindeerr.storage.tables import metadata

EXTRA_REVISION = '''"""Test-only revision."""

import sqlalchemy as sa
from alembic import op

revision = "9999"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("settings", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("settings", "note")
'''


FAILING_REVISION = '''"""Test-only revision that fails halfway."""

import sqlalchemy as sa
from alembic import op

revision = "9999"
down_revision = "{head}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("half", sa.Column("id", sa.Integer(), primary_key=True))
    op.add_column("settings", sa.Column("note", sa.Text(), nullable=True))
    op.execute("INSERT INTO settings VALUES ('ghost', '1', 0, '2026-01-01 00:00:00', NULL)")
    raise RuntimeError("revision failed halfway")


def downgrade() -> None:
    pass
'''


def install_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    newer = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, newer)
    (newer / "versions" / "9999_test.py").write_text(source.format(head=head_revision()))
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", newer)


def schema(db_path: Path) -> list[tuple[object, ...]]:
    return query(db_path, "SELECT type, name, sql FROM sqlite_master ORDER BY name")


def query(db_path: Path, sql: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(db_path)) as connection:
        return connection.execute(sql).fetchall()


def test_fresh_database_is_created_at_head(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    result = upgrade_database(db_path, data_dir / "backups", keep=3)

    assert result.from_revision is None
    assert result.to_revision == head_revision()
    assert result.upgraded
    assert result.backup is None
    assert not (data_dir / "backups").exists()
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    tables = {row[0] for row in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "server_state", "alembic_version"} <= tables
    assert query(db_path, "SELECT id, setup_completed_at FROM server_state") == [(1, None)]
    assert query(db_path, "PRAGMA journal_mode") == [("wal",)]


def test_rerun_is_a_no_op(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    again = upgrade_database(db_path, data_dir / "backups", keep=3)
    assert again.from_revision == again.to_revision == head_revision()
    assert not again.upgraded
    assert again.backup is None
    assert not (data_dir / "backups").exists()
    assert query(db_path, "SELECT count(*) FROM server_state") == [(1,)]


def test_connections_use_the_expected_pragmas(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    engine = create_sync_engine(db_path)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA busy_timeout")) == 5000
            assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
    finally:
        engine.dispose()


def test_tables_match_the_migrated_schema(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    engine = create_sync_engine(db_path)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


def test_upgrade_takes_a_consistent_backup_first(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = database_path(data_dir)
    backups = data_dir / "backups"
    upgrade_database(db_path, backups, keep=3)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO settings VALUES ('server_name', '\"Home\"', 0, '2026-01-01 00:00:00')"
        )

    head = head_revision()
    install_revision(tmp_path, monkeypatch, EXTRA_REVISION)
    result = upgrade_database(db_path, backups, keep=3)

    assert (result.from_revision, result.to_revision) == (head, "9999")
    assert result.backup is not None
    assert result.backup.parent == backups
    assert result.backup.name.endswith(f"-rev-{head}.db")
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600
    assert query(result.backup, "SELECT version_num FROM alembic_version") == [(head,)]
    assert query(result.backup, "SELECT name FROM settings") == [("server_name",)]
    assert query(db_path, "SELECT version_num FROM alembic_version") == [("9999",)]
    assert "note" in {row[1] for row in query(db_path, "PRAGMA table_info(settings)")}


def test_database_from_a_newer_version_is_refused(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("UPDATE alembic_version SET version_num = 'abcdef'")
    with pytest.raises(SchemaTooNewError, match="newer Tindeerr"):
        upgrade_database(db_path, data_dir / "backups", keep=3)
    assert not (data_dir / "backups").exists()


def test_prune_keeps_the_most_recent_backups(tmp_path: Path) -> None:
    names = [f"tindeerr-2026010{day}T000000000000Z-rev-0001.db" for day in range(1, 6)]
    for name in names:
        (tmp_path / name).touch()
    (tmp_path / "unrelated.txt").touch()
    deleted = prune_backups(tmp_path, keep=2)
    assert [path.name for path in deleted] == names[:3]
    assert sorted(path.name for path in tmp_path.iterdir()) == [*names[3:], "unrelated.txt"]


def test_utc_datetime_normalises_to_aware_utc() -> None:
    column_type = UtcDateTime()
    dialect = create_sync_engine(Path(":memory:")).dialect
    paris = datetime(2026, 9, 22, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    stored = column_type.process_bind_param(paris, dialect)
    assert stored == datetime(2026, 9, 22, 12, 0)  # noqa: DTZ001 - stored naive by design
    loaded = column_type.process_result_value(stored, dialect)
    assert loaded == paris
    assert loaded is not None
    assert loaded.tzinfo is UTC
    assert column_type.process_bind_param(None, dialect) is None
    assert column_type.process_result_value(None, dialect) is None
    with pytest.raises(ValueError, match="naive"):
        column_type.process_bind_param(datetime(2026, 1, 1), dialect)  # noqa: DTZ001


def test_alembic_command_line_fallback(tmp_path: Path) -> None:
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    db_path = tmp_path / "dev.db"
    config = Config()
    config.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")
    assert query(db_path, "SELECT version_num FROM alembic_version") == [(head_revision(),)]


def test_initial_revision_downgrades_cleanly(data_dir: Path) -> None:
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    config = Config()
    config.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(config, "base")
    tables = {row[0] for row in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"alembic_version"}


def test_failed_migration_leaves_the_database_unchanged(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO settings VALUES ('server_name', '\"Home\"', 0, '2026-01-01 00:00:00')"
        )
    before = schema(db_path)
    head = head_revision()
    install_revision(tmp_path, monkeypatch, FAILING_REVISION)

    with pytest.raises(RuntimeError, match="halfway"):
        upgrade_database(db_path, data_dir / "backups", keep=3)

    assert schema(db_path) == before
    assert query(db_path, "SELECT version_num FROM alembic_version") == [(head,)]
    assert query(db_path, "SELECT name FROM settings") == [("server_name",)]


def test_failed_first_migration_leaves_an_empty_database(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = database_path(data_dir)
    install_revision(tmp_path, monkeypatch, FAILING_REVISION)
    with pytest.raises(RuntimeError, match="halfway"):
        upgrade_database(db_path, data_dir / "backups", keep=3)
    assert schema(db_path) == []


def test_migration_waits_for_the_lock(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    results: list[MigrationResult] = []
    fd = os.open(data_dir / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        worker = threading.Thread(
            target=lambda: results.append(upgrade_database(db_path, data_dir / "backups", 3))
        )
        worker.start()
        worker.join(0.3)
        assert worker.is_alive()
        assert not db_path.exists()
    finally:
        os.close(fd)
    worker.join(10)
    assert not worker.is_alive()
    assert [result.to_revision for result in results] == [head_revision()]
    assert stat.S_IMODE((data_dir / LOCK_FILE_NAME).stat().st_mode) == 0o600


def test_concurrent_first_starts_migrate_once(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    results: list[MigrationResult] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(upgrade_database(db_path, data_dir / "backups", 3))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    workers = [threading.Thread(target=run) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
    assert errors == []
    assert sorted(result.upgraded for result in results) == [False, False, False, True]


def test_backup_is_verified(tmp_path: Path) -> None:
    good = tmp_path / "good.db"
    with closing(sqlite3.connect(good)) as connection, connection:
        connection.execute("CREATE TABLE t (x INTEGER)")
    verify_backup(good)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(BackupError, match="unreadable"):
        verify_backup(bad)


def test_backup_with_an_inconsistent_index_fails_the_integrity_check(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.db"
    with closing(sqlite3.connect(corrupt)) as connection:
        connection.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
        connection.execute("CREATE INDEX i ON t (a)")
        connection.executemany("INSERT INTO t VALUES (?, ?)", [(n, n + 1000) for n in range(20)])
        connection.commit()
        # Readable file, but the index no longer matches the rows it indexes.
        connection.execute("PRAGMA writable_schema=ON")
        connection.execute(
            "UPDATE sqlite_master SET sql = 'CREATE INDEX i ON t (b)' WHERE name = 'i'"
        )
        connection.commit()
    with pytest.raises(BackupError, match="failed its integrity check"):
        verify_backup(corrupt)


def test_upgrade_is_aborted_when_the_backup_is_bad(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = database_path(data_dir)
    backups = data_dir / "backups"
    upgrade_database(db_path, backups, keep=3)
    head = head_revision()
    install_revision(tmp_path, monkeypatch, EXTRA_REVISION)

    def failing_verify(path: Path) -> None:
        raise BackupError(f"backup {path.name} failed its integrity check")

    monkeypatch.setattr(migrate, "verify_backup", failing_verify)
    with pytest.raises(BackupError, match="integrity"):
        upgrade_database(db_path, backups, keep=3)
    assert list(backups.iterdir()) == []
    assert query(db_path, "SELECT version_num FROM alembic_version") == [(head,)]


def test_backup_io_errors_are_reported(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = database_path(data_dir)
    backups = data_dir / "backups"
    upgrade_database(db_path, backups, keep=3)
    install_revision(tmp_path, monkeypatch, EXTRA_REVISION)

    def failing_fsync(path: Path) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(migrate, "_fsync", failing_fsync)
    with pytest.raises(BackupError, match="cannot back up"):
        upgrade_database(db_path, backups, keep=3)
    assert list(backups.iterdir()) == []


def upgrade_to(db_path: Path, revision: str) -> None:
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    config = Config()
    config.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, revision)


def test_a_step_one_database_is_migrated_and_keeps_its_data(data_dir: Path) -> None:
    db_path = database_path(data_dir)
    backups = data_dir / "backups"
    upgrade_to(db_path, "0001")
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO settings VALUES ('server_name', '\"Home\"', 0, '2026-01-01 00:00:00')"
        )
        connection.execute("UPDATE server_state SET setup_completed_at = '2026-01-01 00:00:00'")

    result = upgrade_database(db_path, backups, keep=3)

    assert (result.from_revision, result.to_revision) == ("0001", head_revision())
    assert result.backup is not None
    assert result.backup.name.endswith("-rev-0001.db")
    tables = {row[0] for row in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"users", "sessions", "refresh_tokens", "pairings"} <= tables
    assert query(db_path, "SELECT name FROM settings") == [("server_name",)]
    ((completed, install_id, code_hash, identity),) = query(
        db_path,
        "SELECT setup_completed_at, install_id, setup_code_hash, media_server_identity "
        "FROM server_state",
    )
    assert completed == "2026-01-01 00:00:00"
    assert isinstance(install_id, str)
    assert len(install_id) >= 16
    assert (code_hash, identity) == (None, None)
    # The single-row constraint survives the table rewrite.
    with (
        pytest.raises(sqlite3.IntegrityError),
        closing(sqlite3.connect(db_path)) as connection,
        connection,
    ):
        connection.execute("INSERT INTO server_state (id, created_at) VALUES (2, '2026-01-01')")


def test_each_database_gets_its_own_install_id(data_dir: Path, tmp_path: Path) -> None:
    first = database_path(data_dir)
    second = tmp_path / "other.db"
    upgrade_database(first, data_dir / "backups", keep=3)
    upgrade_database(second, tmp_path / "backups", keep=3)
    assert query(first, "SELECT install_id FROM server_state") != query(
        second, "SELECT install_id FROM server_state"
    )


def test_the_second_revision_downgrades_cleanly(data_dir: Path) -> None:
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    db_path = database_path(data_dir)
    upgrade_database(db_path, data_dir / "backups", keep=3)
    config = Config()
    config.set_main_option("script_location", str(migrate.MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(config, "0001")
    tables = {row[0] for row in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"settings", "server_state", "alembic_version"}
    columns = {row[1] for row in query(db_path, "PRAGMA table_info(server_state)")}
    assert columns == {"id", "created_at", "setup_completed_at"}
