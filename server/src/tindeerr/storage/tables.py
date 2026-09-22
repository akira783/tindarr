"""Table definitions (SQLAlchemy Core). Migrations are the source of truth for the schema.

A test checks that these definitions and the migrated schema stay identical.
"""

from sqlalchemy import Boolean, CheckConstraint, Column, Integer, MetaData, String, Table, Text

from tindeerr.storage.db import UtcDateTime

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

#: Settings edited by the admin. Secret values are encrypted (``encrypted`` = true).
settings = Table(
    "settings",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("value", Text, nullable=False),
    Column("encrypted", Boolean, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
)

#: Exactly one row describing the instance.
server_state = Table(
    "server_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("created_at", UtcDateTime, nullable=False),
    Column("setup_completed_at", UtcDateTime, nullable=True),
    CheckConstraint("id = 1", name="single_row"),
)
