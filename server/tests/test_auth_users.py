"""Linking a media server account to a Tindarr user (docs/auth.md, section 4)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock, media_user
from tindarr.auth.sessions import SessionService
from tindarr.auth.users import (
    UserUpdate,
    delete_user,
    get_user,
    link_user,
    list_admin_users,
    update_user,
)
from tindarr.core.errors import ProblemError
from tindarr.storage import users as repository
from tindarr.storage.db import write_transaction
from tindarr.storage.users import User

pytestmark = pytest.mark.anyio


async def test_the_first_sign_in_creates_the_user(engine: AsyncEngine, clock: FakeClock) -> None:
    async with write_transaction(engine) as connection:
        user = await link_user(connection, media_user(name="Ada"), clock.now())

    assert user.name == "Ada"
    assert user.media_server_admin is True
    assert user.remote_access is True
    assert user.last_sign_in_at == clock.now()
    assert user.enabled


async def test_later_sign_ins_refresh_what_the_media_server_says(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    async with write_transaction(engine) as connection:
        first = await link_user(connection, media_user(name="Ada"), clock.now())
    clock.advance(timedelta(days=1))
    async with write_transaction(engine) as connection:
        again = await link_user(
            connection,
            media_user(name="Ada Lovelace", admin=False, remote_access=False),
            clock.now(),
        )

    assert again.id == first.id
    assert again.name == "Ada Lovelace"
    assert again.media_server_admin is False
    assert again.remote_access is False
    assert again.last_sign_in_at == clock.now()


async def test_a_user_disabled_here_cannot_sign_in(engine: AsyncEngine, clock: FakeClock) -> None:
    async with write_transaction(engine) as connection:
        user = await link_user(connection, media_user(), clock.now())
        await repository.update_fields(connection, user.id, enabled=False, disabled_reason="admin")
    with pytest.raises(ProblemError) as caught:
        async with write_transaction(engine) as connection:
            await link_user(connection, media_user(), clock.now())
    assert (caught.value.status, caught.value.code) == (403, "account_disabled")


async def test_a_user_the_media_server_had_removed_is_welcomed_back(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    async with write_transaction(engine) as connection:
        user = await link_user(connection, media_user(), clock.now())
        await repository.update_fields(
            connection, user.id, enabled=False, disabled_reason="media_server"
        )
    async with write_transaction(engine) as connection:
        back = await link_user(connection, media_user(), clock.now())
    assert back.id == user.id
    assert back.enabled
    assert back.disabled_reason is None


async def test_an_unlinked_user_keeps_their_row_and_a_new_one_is_created(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    async with write_transaction(engine) as connection:
        old = await link_user(connection, media_user(), clock.now())
        await repository.unlink_all(connection)
    async with write_transaction(engine) as connection:
        fresh = await link_user(connection, media_user(), clock.now())
        everyone = await repository.list_all(connection)
    assert fresh.id != old.id
    assert len(everyone) == 2


# --- what an administrator may change (ADR 0010) --------------------------------------


async def two_users(engine: AsyncEngine, clock: FakeClock) -> tuple[User, User]:
    """A media server administrator and an ordinary user."""
    async with write_transaction(engine) as connection:
        admin = await link_user(connection, media_user(name="Ada"), clock.now())
        other = await link_user(
            connection, media_user(user_id="b" * 32, name="Bo", admin=False), clock.now()
        )
    return admin, other


async def test_promoting_sets_the_tindarr_flag(engine: AsyncEngine, clock: FakeClock) -> None:
    admin, other = await two_users(engine, clock)
    changed = await update_user(
        engine, user_id=other.id, update=UserUpdate(role="admin"), by=admin, now=clock.now()
    )
    assert (changed.promoted, changed.role) == (True, "admin")


async def test_demoting_clears_both_halves(engine: AsyncEngine, clock: FakeClock) -> None:
    admin, other = await two_users(engine, clock)
    await update_user(
        engine, user_id=other.id, update=UserUpdate(role="admin"), by=admin, now=clock.now()
    )
    async with write_transaction(engine) as connection:
        await repository.update_fields(connection, other.id, media_server_admin=True)
    changed = await update_user(
        engine, user_id=other.id, update=UserUpdate(role="user"), by=admin, now=clock.now()
    )
    # The media server's own answer is read again at their next sign-in.
    assert (changed.promoted, changed.media_server_admin, changed.role) == (False, False, "user")


async def test_disabling_revokes_the_sessions_in_the_same_transaction(
    engine: AsyncEngine, clock: FakeClock, sessions: SessionService
) -> None:
    admin, other = await two_users(engine, clock)
    async with write_transaction(engine) as connection:
        grant = await sessions.open_cookie_session(connection, "web", user=other)
    changed = await update_user(
        engine, user_id=other.id, update=UserUpdate(enabled=False), by=admin, now=clock.now()
    )
    assert (changed.enabled, changed.disabled_reason) == (False, "admin")
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_re_enabling_clears_the_reason(engine: AsyncEngine, clock: FakeClock) -> None:
    admin, other = await two_users(engine, clock)
    await update_user(
        engine, user_id=other.id, update=UserUpdate(enabled=False), by=admin, now=clock.now()
    )
    changed = await update_user(
        engine, user_id=other.id, update=UserUpdate(enabled=True), by=admin, now=clock.now()
    )
    assert (changed.enabled, changed.disabled_reason) == (True, None)


async def test_the_generation_limit_tells_null_from_absent(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    admin, other = await two_users(engine, clock)
    limited = await update_user(
        engine,
        user_id=other.id,
        update=UserUpdate(daily_generation_limit=4, limit_given=True),
        by=admin,
        now=clock.now(),
    )
    assert limited.daily_generation_limit == 4
    # An update that does not mention it leaves it alone...
    kept = await update_user(
        engine, user_id=other.id, update=UserUpdate(enabled=True), by=admin, now=clock.now()
    )
    assert kept.daily_generation_limit == 4
    # ...and an explicit null clears it.
    cleared = await update_user(
        engine,
        user_id=other.id,
        update=UserUpdate(limit_given=True),
        by=admin,
        now=clock.now(),
    )
    assert cleared.daily_generation_limit is None


async def test_the_last_enabled_admin_stays(engine: AsyncEngine, clock: FakeClock) -> None:
    admin, _ = await two_users(engine, clock)
    for update in (UserUpdate(role="user"), UserUpdate(enabled=False)):
        with pytest.raises(ProblemError) as refused:
            await update_user(engine, user_id=admin.id, update=update, by=admin, now=clock.now())
        assert (refused.value.status, refused.value.code) == (409, "last_admin")


async def test_a_second_admin_makes_the_first_one_removable(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    admin, other = await two_users(engine, clock)
    await update_user(
        engine, user_id=other.id, update=UserUpdate(role="admin"), by=admin, now=clock.now()
    )
    changed = await update_user(
        engine, user_id=admin.id, update=UserUpdate(role="user"), by=admin, now=clock.now()
    )
    assert changed.role == "user"


async def test_a_promoted_admin_cannot_remove_a_media_server_admin(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    admin, other = await two_users(engine, clock)
    promoted = await update_user(
        engine, user_id=other.id, update=UserUpdate(role="admin"), by=admin, now=clock.now()
    )
    for update in (UserUpdate(role="user"), UserUpdate(enabled=False)):
        with pytest.raises(ProblemError) as refused:
            await update_user(engine, user_id=admin.id, update=update, by=promoted, now=clock.now())
        assert (refused.value.status, refused.value.code) == (403, "forbidden")
    # Something that takes nothing away is still allowed.
    limited = await update_user(
        engine,
        user_id=admin.id,
        update=UserUpdate(daily_generation_limit=1, limit_given=True),
        by=promoted,
        now=clock.now(),
    )
    assert limited.daily_generation_limit == 1


async def test_changing_an_unknown_user_is_not_found(engine: AsyncEngine, clock: FakeClock) -> None:
    admin, _ = await two_users(engine, clock)
    with pytest.raises(ProblemError) as refused:
        await update_user(
            engine, user_id="nobody", update=UserUpdate(enabled=True), by=admin, now=clock.now()
        )
    assert (refused.value.status, refused.value.code) == (404, "not_found")
    with pytest.raises(ProblemError):
        await get_user(engine, "nobody")


async def test_deleting_a_user_takes_their_sessions_with_them(
    engine: AsyncEngine, clock: FakeClock, sessions: SessionService
) -> None:
    admin, other = await two_users(engine, clock)
    async with write_transaction(engine) as connection:
        grant = await sessions.open_cookie_session(connection, "web", user=other)
    await delete_user(engine, other)
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(grant.token, ("web",))
    assert [user.id for user in await list_admin_users(engine)] == [admin.id]


async def test_the_last_admin_cannot_delete_themselves(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    admin, _ = await two_users(engine, clock)
    with pytest.raises(ProblemError) as refused:
        await delete_user(engine, admin)
    assert (refused.value.status, refused.value.code) == (409, "last_admin")


async def test_the_last_admin_rule_reads_the_row_as_it_stands_now(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    # The caller's copy of their row was read when the request was authenticated. A
    # copy that no longer matches the database must not decide whether the rule
    # applies, or somebody promoted a moment ago could delete the last administrator.
    admin, _ = await two_users(engine, clock)
    stale = replace(admin, media_server_admin=False, promoted=False)
    assert not stale.is_admin
    with pytest.raises(ProblemError) as refused:
        await delete_user(engine, stale)
    assert refused.value.code == "last_admin"


async def test_deleting_a_user_that_is_already_gone_does_nothing(
    engine: AsyncEngine, clock: FakeClock
) -> None:
    admin, other = await two_users(engine, clock)
    await delete_user(engine, other)
    await delete_user(engine, other)
    assert [user.id for user in await list_admin_users(engine)] == [admin.id]
