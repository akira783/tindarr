"""Stored batches and cards, votes, jobs, the taste profile and what a generation costs.

The swipe engine stops being a library and becomes something a phone can poll
(roadmap 4.5). A batch and its cards are rows now (ADR 0007), a vote points at a card
id, background work has a state that outlives the process running it, and every
generation is counted against the day it was spent on.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _batches() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("novelty", sa.Text(), nullable=False),
        sa.Column("media_filter", sa.Text(), nullable=False),
        sa.Column("strategy", sa.Text(), server_default="hybrid", nullable=False),
        sa.Column("mood", sa.Text(), nullable=True),
        sa.Column("cards_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("served_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("mode IN ('normal', 'calibration')", name="ck_batches_mode"),
        sa.CheckConstraint(
            "novelty IN ('familiar', 'balanced', 'bold')", name="ck_batches_novelty"
        ),
        sa.CheckConstraint(
            "media_filter IN ('both', 'movie', 'tv')", name="ck_batches_media_filter"
        ),
        sa.CheckConstraint("cards_count >= 0", name="ck_batches_cards_count"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_batches_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_batches"),
    )
    op.create_index("ix_batches_user_id", "batches", ["user_id"])


def _cards() -> None:
    op.create_table(
        "cards",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("batch_id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("pick_type", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("original_title", sa.Text(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("genres", sa.Text(), server_default="[]", nullable=False),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("seasons", sa.Integer(), nullable=True),
        sa.Column("poster_path", sa.Text(), nullable=True),
        sa.Column("backdrop_path", sa.Text(), nullable=True),
        sa.Column("ratings", sa.Text(), server_default="{}", nullable=False),
        sa.Column("providers", sa.Text(), server_default="[]", nullable=False),
        sa.Column("trailer", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("served_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('movie', 'tv')", name="ck_cards_kind"),
        sa.CheckConstraint("tmdb_id > 0", name="ck_cards_tmdb_id"),
        sa.CheckConstraint(
            "pick_type IN ('safe', 'explore', 'calibration')", name="ck_cards_pick_type"
        ),
        sa.CheckConstraint(
            "(served_at IS NULL) = (expires_at IS NULL)", name="ck_cards_expiry_with_served"
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["batches.id"], name="fk_cards_batch_id_batches", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_cards_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cards"),
    )
    op.create_index("ix_cards_batch_id", "cards", ["batch_id"])
    op.create_index("ix_cards_user_id_created_at", "cards", ["user_id", "created_at"])
    op.create_index("ix_cards_user_id_kind_tmdb_id", "cards", ["user_id", "kind", "tmdb_id"])


def _votes() -> None:
    op.create_table(
        "votes",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("card_id", sa.Text(), nullable=True),
        sa.Column("pick_type", sa.Text(), server_default="safe", nullable=False),
        sa.Column("title", sa.Text(), server_default="", nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("poster_path", sa.Text(), nullable=True),
        sa.Column("voted_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("request_status", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('movie', 'tv')", name="ck_votes_kind"),
        sa.CheckConstraint("tmdb_id > 0", name="ck_votes_tmdb_id"),
        sa.CheckConstraint(
            "value IN ('like', 'dislike', 'seen_liked', 'seen_disliked', 'skip')",
            name="ck_votes_value",
        ),
        sa.CheckConstraint(
            "pick_type IN ('safe', 'explore', 'calibration')", name="ck_votes_pick_type"
        ),
        sa.ForeignKeyConstraint(
            ["card_id"], ["cards.id"], name="fk_votes_card_id_cards", ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_votes_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "kind", "tmdb_id", name="pk_votes"),
    )
    op.create_index("ix_votes_user_id_voted_at", "votes", ["user_id", "voted_at"])
    op.create_table(
        "vote_receipts",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("client_vote_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_vote_receipts_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "client_vote_id", name="pk_vote_receipts"),
    )


def _jobs() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("result_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("reported_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("kind IN ('batch', 'profile')", name="ck_jobs_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')", name="ck_jobs_status"
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (error_code IS NOT NULL)", name="ck_jobs_error_with_status"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_jobs_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
    )
    op.create_index("ix_jobs_user_id_created_at", "jobs", ["user_id", "created_at"])


def _per_user() -> None:
    op.create_table(
        "taste_profiles",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("user_edited", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("votes_at_update", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("refresh_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_taste_profiles_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_taste_profiles"),
    )
    op.create_table(
        "preferences",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("media_filter", sa.Text(), server_default="both", nullable=False),
        sa.Column("novelty", sa.Text(), server_default="balanced", nullable=False),
        sa.Column("auto_request", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("streaming_services", sa.Text(), server_default="[]", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "media_filter IN ('both', 'movie', 'tv')", name="ck_preferences_media_filter"
        ),
        sa.CheckConstraint(
            "novelty IN ('familiar', 'balanced', 'bold')", name="ck_preferences_novelty"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_preferences_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_preferences"),
    )


def _usage() -> None:
    op.create_table(
        "llm_usage",
        sa.Column("day", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("generations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_llm_usage_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("day", "user_id", name="pk_llm_usage"),
    )
    op.create_index("ix_llm_usage_day", "llm_usage", ["day"])
    op.create_table(
        "region_providers",
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("providers", sa.Text(), server_default="[]", nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("region", name="pk_region_providers"),
    )


def upgrade() -> None:
    _batches()
    _cards()
    _votes()
    _jobs()
    _per_user()
    _usage()


def downgrade() -> None:
    op.drop_table("region_providers")
    op.drop_index("ix_llm_usage_day", table_name="llm_usage")
    op.drop_table("llm_usage")
    op.drop_table("preferences")
    op.drop_table("taste_profiles")
    op.drop_index("ix_jobs_user_id_created_at", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("vote_receipts")
    op.drop_index("ix_votes_user_id_voted_at", table_name="votes")
    op.drop_table("votes")
    op.drop_index("ix_cards_user_id_kind_tmdb_id", table_name="cards")
    op.drop_index("ix_cards_user_id_created_at", table_name="cards")
    op.drop_index("ix_cards_batch_id", table_name="cards")
    op.drop_table("cards")
    op.drop_index("ix_batches_user_id", table_name="batches")
    op.drop_table("batches")
