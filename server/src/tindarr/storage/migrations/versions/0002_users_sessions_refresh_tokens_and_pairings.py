"""Users, sessions, refresh tokens and pairings; install id and setup state.

Everything step 2 needs to sign people in (docs/auth.md, section 12). The install id is
generated here, once per database: it is the ``DeviceId`` sent to Jellyfin and Emby and
the ``iss`` claim of the access tokens.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""

import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INSTALL_ID_BYTES = 16


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("media_server_user_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("media_server_admin", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("promoted", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("remote_access", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("daily_generation_limit", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_sign_in_at", sa.DateTime(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "disabled_reason IS NULL OR disabled_reason IN ('admin', 'media_server', 'unlinked')",
            name="disabled_reason",
        ),
        sa.CheckConstraint(
            "(enabled = 1) = (disabled_reason IS NULL)",
            name="disabled_reason_matches_enabled",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("media_server_user_id", name="uq_users_media_server_user_id"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("token_hash", sa.Text(), nullable=True),
        sa.Column("csrf_token", sa.Text(), nullable=True),
        sa.Column("device_name", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("app_version", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("reauth_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(kind = 'mobile') = (token_hash IS NULL) AND (kind = 'mobile') = (csrf_token IS NULL)",
            name="cookie_sessions_carry_a_token",
        ),
        sa.CheckConstraint(
            "(kind = 'setup') = (user_id IS NULL)",
            name="user_only_outside_setup",
        ),
        sa.CheckConstraint("kind IN ('mobile', 'web', 'setup')", name="kind"),
        sa.CheckConstraint(
            "revoked_reason IS NULL OR revoked_reason IN ('logout', 'user', 'admin', "
            "'disabled', 'reuse', 'setup_completed', 'superseded', 'server_changed')",
            name="revoked_reason",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="revoked_with_reason",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_user_id_created_at", "sessions", ["user_id", "created_at"])
    op.create_table(
        "refresh_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_refresh_tokens_session_id_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_hash", name="pk_refresh_tokens"),
    )
    op.create_index("ix_refresh_tokens_session_id", "refresh_tokens", ["session_id"])
    op.create_table(
        "pairings",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("code_challenge", sa.Text(), nullable=True),
        sa.Column("device_name", sa.Text(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("app_version", sa.Text(), nullable=True),
        sa.Column("requested_from", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'awaiting_approval', 'approved', 'completed', 'revoked')",
            name="status",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name="fk_pairings_session_id_sessions",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_pairings_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pairings"),
        sa.UniqueConstraint("code_hash", name="uq_pairings_code_hash"),
    )
    op.create_index("ix_pairings_user_id", "pairings", ["user_id"])

    # SQLite cannot add a NOT NULL column without a default: add it nullable, fill the
    # single row, then rewrite the table with the constraint (batch mode).
    with op.batch_alter_table("server_state") as batch:
        batch.add_column(sa.Column("install_id", sa.Text(), nullable=True))
        batch.add_column(sa.Column("setup_code_hash", sa.Text(), nullable=True))
        batch.add_column(sa.Column("media_server_identity", sa.Text(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE server_state SET install_id = :install_id WHERE install_id IS NULL"
        ).bindparams(install_id=secrets.token_urlsafe(_INSTALL_ID_BYTES))
    )
    with op.batch_alter_table("server_state") as batch:
        batch.alter_column("install_id", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("server_state") as batch:
        batch.drop_column("media_server_identity")
        batch.drop_column("setup_code_hash")
        batch.drop_column("install_id")
    op.drop_index("ix_pairings_user_id", table_name="pairings")
    op.drop_table("pairings")
    op.drop_index("ix_refresh_tokens_session_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("ix_sessions_user_id_created_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("users")
