"""Alembic environment.

At runtime ``tindeerr.storage.migrate`` passes an open connection through
``config.attributes["connection"]``. The ``alembic`` command line (development only, see
server/README.md) falls back to ``sqlalchemy.url`` from alembic.ini.
"""

from alembic import context
from sqlalchemy import Connection

from tindeerr.storage.db import create_sync_engine
from tindeerr.storage.tables import metadata


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,  # SQLite cannot ALTER most things; batch mode copies the table
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _main() -> None:
    connection: Connection | None = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    from pathlib import Path  # noqa: PLC0415 - command-line fallback only

    url = context.config.get_main_option("sqlalchemy.url") or "sqlite:///data/tindeerr.db"
    engine = create_sync_engine(Path(url.removeprefix("sqlite:///")))
    try:
        with engine.begin() as conn:
            _run(conn)
    finally:
        engine.dispose()


_main()
