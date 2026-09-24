"""File imports, their review queue, and the watch history they write (roadmap 4.4).

What a person watched outside this deck — a Netflix export, an IMDb ratings file, a tick
on the calibration grid — and the queue of rows an import refused to guess at. None of
the three holds the uploaded file, its name, or anything the parser read beyond the
titles it could not identify.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "imports",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("rows_read", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rows_skipped", sa.Text(), server_default="{}", nullable=False),
        sa.Column("matched", sa.Integer(), server_default="0", nullable=False),
        sa.Column("queued", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("source IN ('netflix', 'imdb', 'letterboxd')", name="ck_imports_source"),
        sa.CheckConstraint("status IN ('running', 'complete', 'failed')", name="ck_imports_status"),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)",
            name="ck_imports_error_with_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_imports_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_imports"),
    )
    op.create_index("ix_imports_user_id", "imports", ["user_id"])
    op.create_index("ix_imports_user_id_created_at", "imports", ["user_id", "created_at"])
    op.create_table(
        "watch_history",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("seen", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("progress", sa.Float(), server_default="0", nullable=False),
        sa.Column("episodes_played", sa.Integer(), nullable=True),
        sa.Column("episodes_total", sa.Integer(), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("last_watched_at", sa.DateTime(), nullable=True),
        sa.Column("import_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('movie', 'tv')", name="ck_watch_history_kind"),
        sa.CheckConstraint("tmdb_id > 0", name="ck_watch_history_tmdb_id"),
        sa.CheckConstraint(
            "source IN ('netflix', 'imdb', 'letterboxd', 'grid')",
            name="ck_watch_history_source",
        ),
        sa.CheckConstraint(
            "state IS NULL OR state IN ('watched', 'mostly_watched', 'in_progress', 'paused', "
            "'abandoned')",
            name="ck_watch_history_state",
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["imports.id"],
            name="fk_watch_history_import_id_imports",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_watch_history_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "source", "kind", "tmdb_id", name="pk_watch_history"),
    )
    op.create_index("ix_watch_history_import_id", "watch_history", ["import_id"])
    op.create_table(
        "import_reviews",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("import_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("kind_hint", sa.Text(), nullable=True),
        sa.Column("episodes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("last_watched_at", sa.DateTime(), nullable=True),
        sa.Column("candidates", sa.Text(), server_default="[]", nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "kind_hint IS NULL OR kind_hint IN ('movie', 'tv')",
            name="ck_import_reviews_kind_hint",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_import_reviews_status"
        ),
        sa.ForeignKeyConstraint(
            ["import_id"],
            ["imports.id"],
            name="fk_import_reviews_import_id_imports",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_import_reviews_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_reviews"),
    )
    op.create_index("ix_import_reviews_import_id", "import_reviews", ["import_id"])
    op.create_index("ix_import_reviews_user_id", "import_reviews", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_import_reviews_user_id", table_name="import_reviews")
    op.drop_index("ix_import_reviews_import_id", table_name="import_reviews")
    op.drop_table("import_reviews")
    op.drop_index("ix_watch_history_import_id", table_name="watch_history")
    op.drop_table("watch_history")
    op.drop_index("ix_imports_user_id_created_at", table_name="imports")
    op.drop_index("ix_imports_user_id", table_name="imports")
    op.drop_table("imports")
