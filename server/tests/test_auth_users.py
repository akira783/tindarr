"""Linking a media server account to a Tindarr user (docs/auth.md, section 4)."""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock, media_user
from tindarr.auth.users import link_user
from tindarr.core.errors import ProblemError
from tindarr.storage import users as repository
from tindarr.storage.db import write_transaction

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
