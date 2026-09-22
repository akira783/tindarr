"""Initial schema: settings and server state.

Revision ID: 0001
Revises:
Create Date: 2026-09-22
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("encrypted", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("name", name="pk_settings"),
    )
    server_state = op.create_table(
        "server_state",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("setup_completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_server_state_single_row"),
        sa.PrimaryKeyConstraint("id", name="pk_server_state"),
    )
    op.bulk_insert(
        server_state,
        [{"id": 1, "created_at": datetime.now(UTC).replace(tzinfo=None)}],
    )


def downgrade() -> None:
    op.drop_table("server_state")
    op.drop_table("settings")
