"""Sessions, tokens and rotation as a service (docs/auth.md, sections 2 and 7)."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock
from tindarr.auth.access import REAUTH_WINDOW, reauth_expires_at
from tindarr.auth.sessions import (
    ABSOLUTE_LIFETIMES,
    IDLE_TIMEOUTS,
    LAST_SEEN_INTERVAL,
    REFRESH_TOKEN_LIFETIME,
    SessionService,
    TokenPair,
)
from tindarr.auth.tokens import AccessTokens, token_hash
from tindarr.core.errors import ProblemError
from tindarr.storage import sessions as session_repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.sessions import Device
from tindarr.storage.users import User

pytestmark = pytest.mark.anyio


async def make_user(engine: AsyncEngine, clock: FakeClock, *, admin: bool = False) -> User:
    async with write_transaction(engine) as connection:
        return await user_repository.insert(
            connection,
            user_repository.new_user(
                f"media-{user_repository.new_id()}", "Alex", clock.now(), admin=admin, remote=True
            ),
        )


async def open_web(
    engine: AsyncEngine, sessions: SessionService, user: User, *, reauthenticated: bool = True
):
    async with write_transaction(engine) as connection:
        return await sessions.open_cookie_session(
            connection,
            "web",
            user=user,
            device=Device("Firefox on Linux", "web"),
            reauthenticated=reauthenticated,
        )


async def open_mobile(engine: AsyncEngine, sessions: SessionService, user: User):
    async with write_transaction(engine) as connection:
        return await sessions.open_mobile_session(
            connection, user, Device("Pixel 9", "android", "1.0.0")
        )


async def test_a_web_session_is_stored_hashed_with_its_own_csrf_token(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)

    assert grant.session.token_hash == token_hash(grant.token)
    assert grant.token not in (grant.session.token_hash, grant.csrf_token)
    assert len(grant.csrf_token) >= 22
    assert grant.session.expires_at == clock.now() + ABSOLUTE_LIFETIMES["web"]
    assert grant.session.device == Device("Firefox on Linux", "web", None)
    assert reauth_expires_at(grant.session, clock.now()) == clock.now() + REAUTH_WINDOW


async def test_two_sessions_never_share_a_token(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    first = await open_web(engine, sessions, user)
    second = await open_web(engine, sessions, user)
    assert first.token != second.token
    assert first.csrf_token != second.csrf_token
    assert first.session.id != second.session.id


async def test_a_cookie_token_authenticates_its_session_and_user(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)

    authenticated = await sessions.authenticate_cookie(grant.token, ("web",))

    assert authenticated.session.id == grant.session.id
    assert authenticated.require_user().id == user.id
    assert authenticated.kind == "web"


async def test_a_cookie_of_another_kind_is_never_accepted(
    engine: AsyncEngine, sessions: SessionService
) -> None:
    async with write_transaction(engine) as connection:
        setup = await sessions.open_cookie_session(connection, "setup")
    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_cookie(setup.token, ("web",))
    authenticated = await sessions.authenticate_cookie(setup.token, ("web", "setup"))
    assert authenticated.kind == "setup"
    assert authenticated.user is None
    with pytest.raises(ProblemError):
        authenticated.require_user()


async def test_an_unknown_cookie_token_is_refused(sessions: SessionService) -> None:
    with pytest.raises(ProblemError) as caught:
        await sessions.authenticate_cookie("nope", ("web",))
    assert (caught.value.status, caught.value.code) == (401, "unauthorized")


async def test_a_revoked_session_stops_working_at_once(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    await sessions.authenticate_cookie(grant.token, ("web",))

    assert await sessions.revoke(grant.session.id, "logout") is True
    assert await sessions.revoke(grant.session.id, "logout") is False

    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_a_disabled_user_stops_working_at_once(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    async with write_transaction(engine) as connection:
        await user_repository.update_fields(
            connection, user.id, enabled=False, disabled_reason="admin"
        )
    with pytest.raises(ProblemError, match="no longer be used"):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_an_unlinked_user_stops_working(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    async with write_transaction(engine) as connection:
        await user_repository.unlink_all(connection)
    with pytest.raises(ProblemError, match="no longer be used"):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_a_session_whose_user_vanished_is_refused(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    async with write_transaction(engine) as connection:
        await connection.execute(
            session_repository.sessions.update()
            .where(session_repository.sessions.c.id == grant.session.id)
            .values(user_id=None, kind="setup")
        )
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_a_web_session_ends_seven_days_after_sign_in(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    for _ in range(13):  # used twice a day, so the idle timeout never applies
        clock.advance(timedelta(hours=12))
        await sessions.authenticate_cookie(grant.token, ("web",))

    clock.advance(timedelta(hours=12))  # seven days after the sign-in

    assert clock.now() == grant.session.created_at + ABSOLUTE_LIFETIMES["web"]
    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_an_app_session_ends_ninety_days_after_sign_in(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    token = grant.tokens.refresh_token
    for _ in range(29):  # refreshed every three days, so it never idles out
        clock.advance(timedelta(days=3))
        token = (await sessions.rotate(token, client_is_private=True)).refresh_token

    clock.advance(timedelta(days=3))  # ninety days after the sign-in

    assert clock.now() == grant.session.created_at + ABSOLUTE_LIFETIMES["mobile"]
    with pytest.raises(ProblemError):
        await sessions.rotate(token, client_is_private=True)


async def test_a_web_session_ends_after_a_day_without_activity(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    idle = IDLE_TIMEOUTS["web"]
    assert idle is not None
    clock.advance(idle - timedelta(minutes=1))
    await sessions.authenticate_cookie(grant.token, ("web",))  # keeps it alive
    clock.advance(idle - timedelta(minutes=1))
    await sessions.authenticate_cookie(grant.token, ("web",))
    clock.advance(idle)
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(grant.token, ("web",))


async def test_a_setup_session_has_no_idle_timeout_and_thirty_minutes_of_life(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    async with write_transaction(engine) as connection:
        grant = await sessions.open_cookie_session(connection, "setup")
    assert grant.session.expires_at == clock.now() + timedelta(minutes=30)
    clock.advance(timedelta(minutes=29))
    await sessions.authenticate_cookie(grant.token, ("setup",))
    clock.advance(timedelta(minutes=2))
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(grant.token, ("setup",))


async def test_last_seen_is_written_at_most_once_a_minute(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user)
    clock.advance(LAST_SEEN_INTERVAL.total_seconds() - 1)
    await sessions.authenticate_cookie(grant.token, ("web",))
    async with engine.connect() as connection:
        unchanged = await session_repository.get(connection, grant.session.id)
    assert unchanged is not None
    assert unchanged.last_seen_at == grant.session.created_at

    clock.advance(LAST_SEEN_INTERVAL)
    await sessions.authenticate_cookie(grant.token, ("web",))
    async with engine.connect() as connection:
        touched = await session_repository.get(connection, grant.session.id)
        seen_user = await user_repository.get(connection, user.id)
    assert touched is not None
    assert touched.last_seen_at == clock.now()
    assert seen_user is not None
    assert seen_user.last_seen_at == clock.now()


async def test_an_app_session_issues_a_working_token_pair(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)

    authenticated = await sessions.authenticate_access_token(grant.tokens.access_token)

    assert authenticated.session.id == grant.session.id
    assert authenticated.require_user().id == user.id
    assert grant.session.token_hash is None
    assert grant.session.csrf_token is None
    assert grant.tokens.refresh_expires_at == clock.now() + REFRESH_TOKEN_LIFETIME


async def test_an_access_token_of_a_revoked_session_is_refused(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    await sessions.revoke(grant.session.id, "user")
    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_access_token(grant.tokens.access_token)


async def test_a_bearer_token_cannot_name_a_web_session(
    engine: AsyncEngine,
    sessions: SessionService,
    access_tokens: AccessTokens,
    clock: FakeClock,
) -> None:
    user = await make_user(engine, clock)
    web = await open_web(engine, sessions, user)
    # A perfectly signed token whose "sid" is a console session: still refused.
    forged = access_tokens.issue(user.id, web.session.id)
    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_access_token(forged.value)


async def test_an_access_token_naming_another_user_is_refused(
    engine: AsyncEngine,
    sessions: SessionService,
    access_tokens: AccessTokens,
    clock: FakeClock,
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    forged = access_tokens.issue("someone-else", grant.session.id)
    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_access_token(forged.value)


async def test_rotation_returns_a_new_pair_and_kills_the_old_token(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)

    clock.advance(60)
    rotated = await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)

    assert isinstance(rotated, TokenPair)
    assert rotated.refresh_token != grant.tokens.refresh_token
    assert rotated.access_token != grant.tokens.access_token
    authenticated = await sessions.authenticate_access_token(rotated.access_token)
    assert authenticated.session.id == grant.session.id
    again = await sessions.rotate(rotated.refresh_token, client_is_private=True)
    assert again.refresh_token != rotated.refresh_token


async def test_reusing_a_refresh_token_revokes_the_session(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    rotated = await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)

    with pytest.raises(ProblemError) as caught:
        await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)

    assert (caught.value.status, caught.value.code) == (401, "refresh_token_reused")
    async with engine.connect() as connection:
        session = await session_repository.get(connection, grant.session.id)
    assert session is not None
    assert session.revoked_reason == "reuse"
    # The thief's token is dead too: the whole session is gone.
    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.rotate(rotated.refresh_token, client_is_private=True)


async def test_two_concurrent_rotations_leave_exactly_one_winner(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)

    async def rotate() -> TokenPair | ProblemError:
        try:
            return await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)
        except ProblemError as problem:
            return problem

    first, second = await asyncio.gather(rotate(), rotate())
    pairs = [result for result in (first, second) if isinstance(result, TokenPair)]
    problems = [result for result in (first, second) if isinstance(result, ProblemError)]
    assert len(pairs) == 1
    assert [problem.code for problem in problems] == ["refresh_token_reused"]


async def test_an_unknown_or_expired_refresh_token_is_unauthorized(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    with pytest.raises(ProblemError) as unknown:
        await sessions.rotate("not-a-token", client_is_private=True)
    assert unknown.value.code == "unauthorized"

    clock.advance(REFRESH_TOKEN_LIFETIME + timedelta(days=1))
    with pytest.raises(ProblemError) as expired:
        await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)
    assert expired.value.code == "unauthorized"


async def test_refreshing_applies_the_remote_access_rule(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    async with write_transaction(engine) as connection:
        await user_repository.update_fields(connection, user.id, remote_access=False)

    with pytest.raises(ProblemError) as caught:
        await sessions.rotate(grant.tokens.refresh_token, client_is_private=False)
    assert (caught.value.status, caught.value.code) == (403, "remote_access_denied")
    # From the local network the same token still works, and was not consumed.
    assert await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)


async def test_refreshing_a_disabled_user_fails(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    async with write_transaction(engine) as connection:
        await user_repository.update_fields(
            connection, user.id, enabled=False, disabled_reason="media_server"
        )
    with pytest.raises(ProblemError, match="no longer be used"):
        await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)


async def test_a_refresh_token_never_outlives_its_session(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_mobile(engine, sessions, user)
    clock.advance(timedelta(days=59))
    rotated = await sessions.rotate(grant.tokens.refresh_token, client_is_private=True)
    assert rotated.refresh_expires_at == grant.session.expires_at
    assert rotated.refresh_expires_at < clock.now() + REFRESH_TOKEN_LIFETIME


async def test_revoking_a_user_can_keep_the_current_session(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    kept = await open_web(engine, sessions, user)
    other = await open_web(engine, sessions, user)

    assert await sessions.revoke_user_sessions(user.id, "admin", kept.session.id) == 1

    await sessions.authenticate_cookie(kept.token, ("web",))
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(other.token, ("web",))


async def test_marking_a_session_reauthenticated(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    grant = await open_web(engine, sessions, user, reauthenticated=False)
    assert reauth_expires_at(grant.session, clock.now()) is None

    expires_at = await sessions.mark_reauthenticated(grant.session.id)

    assert expires_at == clock.now() + REAUTH_WINDOW
    authenticated = await sessions.authenticate_cookie(grant.token, ("web",))
    assert reauth_expires_at(authenticated.session, clock.now()) == expires_at
    clock.advance(REAUTH_WINDOW + timedelta(seconds=1))
    authenticated = await sessions.authenticate_cookie(grant.token, ("web",))
    assert reauth_expires_at(authenticated.session, clock.now()) is None


async def test_the_purge_removes_finished_sessions_only(
    engine: AsyncEngine, sessions: SessionService, clock: FakeClock
) -> None:
    user = await make_user(engine, clock)
    live = await open_web(engine, sessions, user)
    old = await open_web(engine, sessions, user)
    await sessions.revoke(old.session.id, "logout")

    clock.advance(timedelta(days=31))
    result = await sessions.purge()

    assert (result.sessions, result.pairings) == (1, 0)
    async with engine.connect() as connection:
        assert await session_repository.get(connection, old.session.id) is None
        assert await session_repository.get(connection, live.session.id) is not None
