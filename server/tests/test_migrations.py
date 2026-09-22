import shutil
import sqlite3
import stat
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
    SchemaTooNewError,
    head_revision,
    prune_backups,
    upgrade_database,
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

    newer = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, newer)
    (newer / "versions" / "9999_test.py").write_text(EXTRA_REVISION.format(head=head_revision()))
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", newer)

    result = upgrade_database(db_path, backups, keep=3)

    assert (result.from_revision, result.to_revision) == ("0001", "9999")
    assert result.backup is not None
    assert result.backup.parent == backups
    assert result.backup.name.endswith("-rev-0001.db")
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600
    assert query(result.backup, "SELECT version_num FROM alembic_version") == [("0001",)]
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
