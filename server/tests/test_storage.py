"""Rows and queries of ``users``, ``sessions``, ``refresh_tokens`` and ``server_state``."""

import asyncio
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncEngine

from tindeerr.storage import server_state as state_repo
from tindeerr.storage import sessions as sessions_repo
from tindeerr.storage import users as users_repo
from tindeerr.storage.db import write_transaction
from tindeerr.storage.sessions import Device, Session, SessionLifetime
from tindeerr.storage.tables import pairings, server_state, sessions, users
from tindeerr.storage.users import User

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("row_type", "table"),
    [(User, users), (Session, sessions), (state_repo.ServerState, server_state)],
    ids=["users", "sessions", "server_state"],
)
@pytest.mark.anyio
async def test_row_types_match_their_table_column_by_column(row_type: type, table: Table) -> None:
    # Rows are built positionally from a SELECT *, so the order must stay identical.
    assert [field.name for field in fields(row_type)] == [column.name for column in table.columns]


async def make_user(engine: AsyncEngine, *, admin: bool = False, remote: bool = True) -> User:
    async with write_transaction(engine) as connection:
        user = users_repo.new_user(
            f"media-{users_repo.new_id()}", "Alex", NOW, admin=admin, remote=remote
        )
        return await users_repo.insert(connection, user)


async def make_session(
    engine: AsyncEngine, user: User | None = None, kind: sessions_repo.SessionKind = "web"
) -> Session:
    cookie = kind != "mobile"
    lifetime = SessionLifetime(
        NOW,
        NOW + timedelta(days=7),
        token_hash=users_repo.new_id() if cookie else None,
        csrf_token=users_repo.new_id() if cookie else None,
    )
    async with write_transaction(engine) as connection:
        session = sessions_repo.new_session(
            kind,
            lifetime,
            user_id=None if user is None else user.id,
            device=Device("Firefox on Linux", "web"),
        )
        return await sessions_repo.insert(connection, session)


async def test_user_round_trip(engine: AsyncEngine) -> None:
    created = await make_user(engine, admin=True)
    async with engine.connect() as connection:
        loaded = await users_repo.get(connection, created.id)
        by_media_id = await users_repo.get_by_media_server_id(
            connection, created.media_server_user_id or ""
        )
        everyone = await users_repo.list_all(connection)
    assert loaded == created
    assert by_media_id == created
    assert list(everyone) == [created]
    assert created.role == "admin"
    assert created.is_admin
    assert created.linked
    assert created.can_sign_in


async def test_unknown_user_is_none(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert await users_repo.get(connection, "nobody") is None
        assert await users_repo.get_by_media_server_id(connection, "nobody") is None


async def test_promotion_and_the_last_admin_count(engine: AsyncEngine) -> None:
    plain = await make_user(engine)
    await make_user(engine, admin=True)
    async with write_transaction(engine) as connection:
        assert await users_repo.count_enabled_admins(connection) == 1
        await users_repo.update_fields(connection, plain.id, promoted=True)
        assert await users_repo.count_enabled_admins(connection) == 2
        promoted = await users_repo.get(connection, plain.id)
    assert promoted is not None
    assert promoted.role == "admin"


async def test_touch_records_the_last_seen_time(engine: AsyncEngine) -> None:
    user = await make_user(engine)
    async with write_transaction(engine) as connection:
        await users_repo.touch(connection, user.id, NOW)
        seen = await users_repo.get(connection, user.id)
    assert seen is not None
    assert seen.last_seen_at == NOW


async def test_unlinking_disables_every_user_and_keeps_their_row(engine: AsyncEngine) -> None:
    await make_user(engine)
    await make_user(engine, admin=True)
    async with write_transaction(engine) as connection:
        assert await users_repo.unlink_all(connection) == 2
        everyone = await users_repo.list_all(connection)
    assert [user.disabled_reason for user in everyone] == ["unlinked", "unlinked"]
    assert not any(user.enabled or user.linked or user.can_sign_in for user in everyone)


async def test_a_disabled_user_needs_a_reason(engine: AsyncEngine) -> None:
    user = await make_user(engine)
    with pytest.raises(Exception, match="disabled_reason_matches_enabled"):
        async with write_transaction(engine) as connection:
            await users_repo.update_fields(connection, user.id, enabled=False)


async def test_session_round_trip_and_device(engine: AsyncEngine) -> None:
    user = await make_user(engine)
    created = await make_session(engine, user)
    async with engine.connect() as connection:
        loaded = await sessions_repo.get(connection, created.id)
        by_hash = await sessions_repo.get_by_token_hash(connection, created.token_hash or "")
        listed = await sessions_repo.list_for_user(connection, user.id)
    assert loaded == created
    assert by_hash == created
    assert [session.id for session in listed] == [created.id]
    assert created.device == Device("Firefox on Linux", "web", None)


async def test_unknown_session_is_none(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        assert await sessions_repo.get(connection, "nope") is None
        assert await sessions_repo.get_by_token_hash(connection, "nope") is None


async def test_revocation_is_a_compare_and_set(engine: AsyncEngine) -> None:
    session = await make_session(engine, await make_user(engine))
    async with write_transaction(engine) as connection:
        assert await sessions_repo.revoke(connection, session.id, "logout", NOW) is True
        assert await sessions_repo.revoke(connection, session.id, "admin", NOW) is False
        revoked = await sessions_repo.get(connection, session.id)
    assert revoked is not None
    assert (revoked.revoked_at, revoked.revoked_reason) == (NOW, "logout")


async def test_revoking_by_kind_user_and_everything(engine: AsyncEngine) -> None:
    user = await make_user(engine)
    other = await make_user(engine)
    kept = await make_session(engine, user)
    await make_session(engine, user)
    await make_session(engine, other)
    setup = await make_session(engine, kind="setup")
    async with write_transaction(engine) as connection:
        assert await sessions_repo.revoke_for_user(connection, user.id, "admin", NOW, kept.id) == 1
        assert await sessions_repo.revoke_of_kind(connection, "setup", "superseded", NOW) == 1
        assert await sessions_repo.revoke_all(connection, "server_changed", NOW) == 2
        live = await sessions_repo.list_for_user(connection, user.id)
        gone = await sessions_repo.get(connection, setup.id)
    assert live == []
    assert gone is not None
    assert gone.revoked_reason == "superseded"


async def test_sessions_of_a_deleted_user_go_with_them(engine: AsyncEngine) -> None:
    user = await make_user(engine)
    session = await make_session(engine, user)
    async with write_transaction(engine) as connection:
        await connection.execute(users.delete().where(users.c.id == user.id))
        assert await sessions_repo.get(connection, session.id) is None


async def test_refresh_token_rotation_claims_once(engine: AsyncEngine) -> None:
    session = await make_session(engine, await make_user(engine), kind="mobile")
    async with write_transaction(engine) as connection:
        await sessions_repo.add_refresh_token(
            connection,
            session_id=session.id,
            token_hash="hash-1",
            now=NOW,
            expires_at=NOW + timedelta(days=60),
        )
    async with write_transaction(engine) as connection:
        first = await sessions_repo.claim_refresh_token(connection, "hash-1", NOW)
    async with write_transaction(engine) as connection:
        again = await sessions_repo.claim_refresh_token(connection, "hash-1", NOW)
        unknown = await sessions_repo.claim_refresh_token(connection, "hash-0", NOW)
    assert first == sessions_repo.RefreshOutcome(claimed=True, reused=False, session_id=session.id)
    assert again == sessions_repo.RefreshOutcome(claimed=False, reused=True, session_id=session.id)
    assert unknown == sessions_repo.RefreshOutcome(claimed=False, reused=False)


async def test_an_expired_refresh_token_is_not_claimed(engine: AsyncEngine) -> None:
    session = await make_session(engine, await make_user(engine), kind="mobile")
    async with write_transaction(engine) as connection:
        await sessions_repo.add_refresh_token(
            connection,
            session_id=session.id,
            token_hash="stale",
            now=NOW - timedelta(days=90),
            expires_at=NOW - timedelta(days=30),
        )
        outcome = await sessions_repo.claim_refresh_token(connection, "stale", NOW)
    assert outcome == sessions_repo.RefreshOutcome(claimed=False, reused=False)


async def test_two_concurrent_rotations_of_one_token_leave_exactly_one_winner(
    engine: AsyncEngine,
) -> None:
    session = await make_session(engine, await make_user(engine), kind="mobile")
    async with write_transaction(engine) as connection:
        await sessions_repo.add_refresh_token(
            connection,
            session_id=session.id,
            token_hash="racy",
            now=NOW,
            expires_at=NOW + timedelta(days=60),
        )

    async def rotate() -> sessions_repo.RefreshOutcome:
        async with write_transaction(engine) as connection:
            outcome = await sessions_repo.claim_refresh_token(connection, "racy", NOW)
            await asyncio.sleep(0.02)  # give the other rotation every chance to interleave
            return outcome

    outcomes = await asyncio.gather(rotate(), rotate())
    assert sorted(outcome.claimed for outcome in outcomes) == [False, True]
    assert sorted(outcome.reused for outcome in outcomes) == [False, True]


async def test_purge_removes_finished_sessions_and_old_pairings(engine: AsyncEngine) -> None:
    user = await make_user(engine)
    live = await make_session(engine, user)
    revoked = await make_session(engine, user)
    expired = await make_session(engine, user)
    async with write_transaction(engine) as connection:
        await sessions_repo.add_refresh_token(
            connection,
            session_id=revoked.id,
            token_hash="old",
            now=NOW - timedelta(days=90),
            expires_at=NOW - timedelta(days=30),
        )
        await connection.execute(
            sessions.update()
            .where(sessions.c.id == revoked.id)
            .values(revoked_at=NOW - timedelta(days=40), revoked_reason="logout")
        )
        await connection.execute(
            sessions.update()
            .where(sessions.c.id == expired.id)
            .values(expires_at=NOW - timedelta(days=40))
        )
        for age, identifier in ((10, "old-pairing"), (1, "fresh-pairing")):
            await connection.execute(
                pairings.insert().values(
                    id=identifier,
                    user_id=user.id,
                    code_hash=identifier,
                    status="pending",
                    created_at=NOW - timedelta(days=age),
                    expires_at=NOW - timedelta(days=age) + timedelta(minutes=5),
                )
            )
    async with write_transaction(engine) as connection:
        removed = await sessions_repo.purge(
            connection,
            sessions_before=NOW - timedelta(days=30),
            pairings_before=NOW - timedelta(days=7),
        )
        left = (await connection.execute(select(sessions.c.id))).scalars().all()
        tokens = (await connection.execute(select(sessions_repo.refresh_tokens.c.token_hash))).all()
        remaining_pairings = (await connection.execute(select(pairings.c.id))).scalars().all()
    assert removed == (2, 1)
    assert list(left) == [live.id]
    assert tokens == []
    assert list(remaining_pairings) == ["fresh-pairing"]


async def test_setup_completion_is_a_compare_and_set(engine: AsyncEngine) -> None:
    async with write_transaction(engine) as connection:
        await state_repo.set_setup_code_hash(connection, "hash")
        before = await state_repo.read(connection)
        assert await state_repo.complete_setup(connection, NOW) is True
        assert await state_repo.complete_setup(connection, NOW) is False
        after = await state_repo.read(connection)
    assert before.install_id
    assert not before.setup_completed
    assert after.setup_completed
    assert after.setup_code_hash is None


async def test_identity_and_reopening_setup(engine: AsyncEngine) -> None:
    async with write_transaction(engine) as connection:
        await state_repo.set_media_server_identity(connection, "jellyfin:abc")
        await state_repo.complete_setup(connection, NOW)
        configured = await state_repo.read(connection)
        await state_repo.reopen_setup(connection, "new-hash")
        reset = await state_repo.read(connection)
    assert configured.media_server_identity == "jellyfin:abc"
    assert not reset.setup_completed
    assert (reset.media_server_identity, reset.setup_code_hash) == (None, "new-hash")
    assert reset.install_id == configured.install_id


async def test_repository_reads_the_state(engine: AsyncEngine) -> None:
    repository = state_repo.ServerStateRepository(engine)
    assert await repository.setup_completed() is False
    async with write_transaction(engine) as connection:
        await state_repo.complete_setup(connection, NOW)
    assert await repository.setup_completed() is True
    assert (await repository.read()).install_id
