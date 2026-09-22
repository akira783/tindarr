"""First-run setup: the code, the claim, the wizard and the completion (auth.md, §3)."""

import logging
import stat
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock, FakeMediaServer, FakeMediaServers, media_user
from tindeerr.auth.mediaserver import MediaServerConnector, MediaServerInput
from tindeerr.auth.ratelimit import RateLimits
from tindeerr.auth.sessions import SessionService
from tindeerr.auth.setup import SetupService
from tindeerr.auth.setupcode import ALPHABET, CODE_LENGTH, read_setup_code, setup_code_hash
from tindeerr.core.crypto import SecretCipher
from tindeerr.core.errors import ProblemError
from tindeerr.core.keys import KeyMaterial
from tindeerr.storage import server_state as state_repository
from tindeerr.storage import sessions as session_repository
from tindeerr.storage import users as users_repository
from tindeerr.storage.db import write_transaction
from tindeerr.storage.sessions import Device
from tindeerr.storage.settings import SettingsStore

pytestmark = pytest.mark.anyio

JELLYFIN = MediaServerInput(
    kind="jellyfin",
    url="http://jellyfin.lan:8096",
    api_key="admin-api-key",
    given=frozenset({"server_type", "url", "api_key"}),
)


async def code_of(setup_service: SetupService) -> str:
    path = await setup_service.ensure_setup_code()
    assert path is not None
    code = read_setup_code(path)
    assert code is not None
    return code


async def open_setup_session(engine: AsyncEngine, sessions: SessionService):
    """Open a setup session directly, without going through the claim."""
    async with write_transaction(engine) as connection:
        return await sessions.open_cookie_session(connection, "setup")


async def configure(
    setup_service: SetupService, engine: AsyncEngine, sessions: SessionService
) -> None:
    """Configure the fake media server, as the wizard's second step does."""
    grant = await open_setup_session(engine, sessions)
    await setup_service.configure_media_server(grant.session, JELLYFIN)


# --- the code -----------------------------------------------------------------------


async def test_the_code_is_written_once_with_mode_600_and_only_its_hash_is_stored(
    setup_service: SetupService, engine: AsyncEngine, data_dir: Path
) -> None:
    path = await setup_service.ensure_setup_code()

    assert path is not None
    assert path == data_dir / "setup-code"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    code = read_setup_code(path)
    assert code is not None
    assert len(code) == CODE_LENGTH
    assert set(code) <= set(ALPHABET)
    async with engine.connect() as connection:
        state = await state_repository.read(connection)
    assert state.setup_code_hash == setup_code_hash(code)
    assert code not in str(state.setup_code_hash)


async def test_the_code_is_never_logged_only_its_path(
    setup_service: SetupService, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        path = await setup_service.ensure_setup_code()
    assert path is not None
    code = read_setup_code(path)
    assert code is not None
    assert str(path) in caplog.text
    assert code not in caplog.text


async def test_a_restart_keeps_the_same_code(setup_service: SetupService) -> None:
    first = await code_of(setup_service)
    assert await code_of(setup_service) == first


async def test_deleting_the_file_generates_a_new_code(
    setup_service: SetupService, engine: AsyncEngine
) -> None:
    first = await code_of(setup_service)
    setup_service.code_path.unlink()

    second = await code_of(setup_service)

    assert second != first
    async with engine.connect() as connection:
        state = await state_repository.read(connection)
    assert state.setup_code_hash == setup_code_hash(second)


async def test_a_code_file_that_does_not_match_the_stored_hash_is_replaced(
    setup_service: SetupService, caplog: pytest.LogCaptureFixture
) -> None:
    await code_of(setup_service)
    setup_service.code_path.write_text("0000000000ZZ\n")

    with caplog.at_level(logging.WARNING):
        replaced = await code_of(setup_service)

    assert replaced != "0000000000ZZ"
    assert "does not match" in caplog.text


async def test_completing_setup_deletes_the_code_file(
    setup_service: SetupService,
    engine: AsyncEngine,
    clock: FakeClock,
    sessions: SessionService,
) -> None:
    await code_of(setup_service)
    await configure(setup_service, engine, sessions)
    grant = await setup_service.claim(await code_of(setup_service), client_key="10.0.0.1")

    await setup_service.complete_setup(grant.session, media_user())

    assert not setup_service.code_path.exists()
    async with engine.connect() as connection:
        state = await state_repository.read(connection)
    assert state.setup_code_hash is None
    assert state.setup_completed_at == clock.now()
    # A later start does not write it again.
    assert await setup_service.ensure_setup_code() is None
    assert not setup_service.code_path.exists()


# --- the claim ----------------------------------------------------------------------


async def test_the_claim_opens_a_setup_session(
    setup_service: SetupService, clock: FakeClock
) -> None:
    code = await code_of(setup_service)

    grant = await setup_service.claim(code, client_key="10.0.0.1")

    assert grant.session.kind == "setup"
    assert grant.session.user_id is None
    assert grant.csrf_token
    assert grant.session.expires_at == clock.now() + timedelta(minutes=30)


@pytest.mark.parametrize("typed", ["{code}", "{lower}", "{spaced}", "{dashed}"])
async def test_the_code_is_read_forgivingly(setup_service: SetupService, typed: str) -> None:
    code = await code_of(setup_service)
    variants = {
        "code": code,
        "lower": code.lower(),
        "spaced": f" {code} ",
        "dashed": f"{code[:4]}-{code[4:8]}-{code[8:]}",
    }
    assert await setup_service.claim(typed.format(**variants), client_key="10.0.0.1")


async def test_a_wrong_code_is_refused_and_counted(setup_service: SetupService) -> None:
    await code_of(setup_service)
    with pytest.raises(ProblemError) as caught:
        await setup_service.claim("00000000000Z", client_key="10.0.0.1")
    assert (caught.value.status, caught.value.code) == (401, "invalid_setup_code")


async def test_the_code_works_several_times_until_setup_completes(
    setup_service: SetupService,
) -> None:
    code = await code_of(setup_service)
    first = await setup_service.claim(code, client_key="10.0.0.1")
    second = await setup_service.claim(code, client_key="10.0.0.1")
    assert first.session.id != second.session.id


async def test_a_new_claim_revokes_the_previous_setup_session(
    setup_service: SetupService, sessions: SessionService, engine: AsyncEngine
) -> None:
    code = await code_of(setup_service)
    first = await setup_service.claim(code, client_key="10.0.0.1")

    second = await setup_service.claim(code, client_key="192.168.1.9")

    with pytest.raises(ProblemError, match="No valid credential"):
        await sessions.authenticate_cookie(first.token, ("setup",))
    assert await sessions.authenticate_cookie(second.token, ("setup",))
    async with engine.connect() as connection:
        revoked = await session_repository.get(connection, first.session.id)
    assert revoked is not None
    assert revoked.revoked_reason == "superseded"


async def test_claims_are_limited_per_client_address(
    setup_service: SetupService, limits: RateLimits
) -> None:
    await code_of(setup_service)
    for _ in range(limits.claim_failures.limit.count):
        with pytest.raises(ProblemError, match="Wrong setup code"):
            await setup_service.claim("00000000000Z", client_key="10.0.0.1")

    with pytest.raises(ProblemError) as caught:
        await setup_service.claim("00000000000Z", client_key="10.0.0.1")

    assert caught.value.code == "rate_limited"
    # Another address is not affected by this one's failures.
    with pytest.raises(ProblemError, match="Wrong setup code"):
        await setup_service.claim("00000000000Z", client_key="10.0.0.2")


async def test_too_many_claims_everywhere_only_slow_the_server_down(
    setup_service: SetupService, limits: RateLimits, clock: FakeClock
) -> None:
    code = await code_of(setup_service)
    for index in range(limits.claim_failures_global.limit.count):
        with pytest.raises(ProblemError, match="Wrong setup code"):
            await setup_service.claim("00000000000Z", client_key=f"10.0.0.{index}")

    assert await setup_service.claim(code, client_key="192.168.1.9")
    assert clock.slept == [1.0]


async def test_claiming_a_server_already_set_up_is_a_conflict(
    setup_service: SetupService, engine: AsyncEngine, clock: FakeClock
) -> None:
    code = await code_of(setup_service)
    async with write_transaction(engine) as connection:
        await state_repository.complete_setup(connection, clock.now())
    with pytest.raises(ProblemError) as caught:
        await setup_service.claim(code, client_key="10.0.0.1")
    assert (caught.value.status, caught.value.code) == (409, "setup_completed")


async def test_claiming_without_a_stored_code_is_refused(
    setup_service: SetupService,
) -> None:
    with pytest.raises(ProblemError, match="Wrong setup code"):
        await setup_service.claim("00000000000Z", client_key="10.0.0.1")


# --- the wizard's state -------------------------------------------------------------


async def test_the_state_is_empty_before_the_media_server_is_configured(
    setup_service: SetupService,
) -> None:
    state = await setup_service.state(client_is_private=True)
    assert state.media_server is None
    assert state.auth_methods == []
    assert state.media_server_locked is False
    assert state.locked_fields == []


async def test_the_state_lists_the_methods_of_the_configured_server(
    setup_service: SetupService,
    engine: AsyncEngine,
    sessions: SessionService,
) -> None:
    await configure(setup_service, engine, sessions)
    state = await setup_service.state(client_is_private=True)
    assert state.media_server == "jellyfin"
    # Quick Connect needs the adapter's cache (step 2b), so only the password shows.
    assert state.auth_methods == ["password"]


async def test_the_state_follows_password_sign_in(
    setup_service: SetupService,
    engine: AsyncEngine,
    settings_store: SettingsStore,
    sessions: SessionService,
) -> None:
    await configure(setup_service, engine, sessions)
    await settings_store.set("password_sign_in", "lan_only")
    assert (await setup_service.state(client_is_private=True)).auth_methods == ["password"]
    assert (await setup_service.state(client_is_private=False)).auth_methods == []
    await settings_store.set("password_sign_in", "disabled")
    assert (await setup_service.state(client_is_private=True)).auth_methods == []


async def test_environment_variables_lock_the_media_server_step(  # noqa: PLR0913, PLR0917
    engine: AsyncEngine,
    data_dir: Path,
    clock: FakeClock,
    sessions: SessionService,
    keys: KeyMaterial,
    media_servers: FakeMediaServers,
    limits: RateLimits,
) -> None:
    settings = SettingsStore(
        engine,
        SecretCipher.for_settings(keys),
        {
            "media_server_kind": "emby",
            "media_server_url": "http://emby.lan:8096",
            "media_server_api_key": "from-the-environment",
        },
    )
    connector = MediaServerConnector(engine, settings, media_servers)
    service = SetupService(engine, data_dir, clock, sessions, connector, settings, limits)

    state = await service.state(client_is_private=True)

    assert state.media_server == "emby"
    assert state.media_server_locked is True
    assert sorted(state.locked_fields) == ["api_key", "server_type", "url"]


# --- the media server step ----------------------------------------------------------


async def test_configuring_the_media_server_tests_it_and_stores_its_identity(
    setup_service: SetupService,
    engine: AsyncEngine,
    media_servers: FakeMediaServers,
    settings_store: SettingsStore,
    sessions: SessionService,
) -> None:
    async with engine.connect() as connection:
        state = await state_repository.read(connection)
    grant = await open_setup_session(engine, sessions)

    check = await setup_service.configure_media_server(grant.session, JELLYFIN)

    assert check.ok
    assert check.server_name == "Home Jellyfin"
    assert media_servers.server.calls == ["test", "identify"]
    assert media_servers.last.device_id == state.install_id
    assert media_servers.last.secret == "admin-api-key"
    async with engine.connect() as connection:
        stored = await state_repository.read(connection)
    assert stored.media_server_identity == "jellyfin:server-1"
    assert (await settings_store.get("media_server_url")).value == "http://jellyfin.lan:8096"
    assert (await settings_store.get("media_server_api_key")).value == "admin-api-key"


async def test_a_failed_connection_test_saves_nothing(
    setup_service: SetupService,
    engine: AsyncEngine,
    media_servers: FakeMediaServers,
    settings_store: SettingsStore,
    sessions: SessionService,
) -> None:
    media_servers.server = FakeMediaServer(health="unauthorized")
    grant = await open_setup_session(engine, sessions)

    with pytest.raises(ProblemError) as caught:
        await setup_service.configure_media_server(grant.session, JELLYFIN)

    assert (caught.value.status, caught.value.code) == (502, "connector_unauthorized")
    assert (await settings_store.get("media_server_url")).value is None
    async with engine.connect() as connection:
        assert (await state_repository.read(connection)).media_server_identity is None


async def test_a_server_answering_as_another_product_is_refused(
    setup_service: SetupService,
    engine: AsyncEngine,
    media_servers: FakeMediaServers,
    sessions: SessionService,
) -> None:
    media_servers.server = FakeMediaServer(identifies_as="emby")
    grant = await open_setup_session(engine, sessions)
    with pytest.raises(ProblemError) as caught:
        await setup_service.configure_media_server(grant.session, JELLYFIN)
    assert (caught.value.status, caught.value.code) == (502, "media_server_unsupported")


async def test_connection_tests_are_limited_per_session(
    setup_service: SetupService,
    engine: AsyncEngine,
    limits: RateLimits,
    sessions: SessionService,
) -> None:
    grant = await open_setup_session(engine, sessions)
    for _ in range(limits.connection_tests.limit.count):
        await setup_service.configure_media_server(grant.session, JELLYFIN)
    with pytest.raises(ProblemError) as caught:
        await setup_service.configure_media_server(grant.session, JELLYFIN)
    assert caught.value.code == "rate_limited"


# --- completion ---------------------------------------------------------------------


async def test_completing_setup_creates_the_admin_and_a_new_web_session(
    setup_service: SetupService,
    engine: AsyncEngine,
    sessions: SessionService,
    clock: FakeClock,
) -> None:
    await configure(setup_service, engine, sessions)
    claim = await setup_service.claim(await code_of(setup_service), client_key="10.0.0.1")

    completed = await setup_service.complete_setup(
        claim.session, media_user(name="Ada"), device=Device("Firefox on Linux", "web")
    )

    assert completed.user.name == "Ada"
    assert completed.user.media_server_admin is True
    assert completed.user.is_admin
    assert completed.grant.session.kind == "web"
    assert completed.grant.session.id != claim.session.id
    assert completed.grant.token != claim.token
    assert completed.grant.csrf_token != claim.csrf_token
    # The new session is freshly re-authenticated, and the setup session is gone.
    assert completed.grant.session.reauth_at == clock.now()
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(claim.token, ("setup",))
    authenticated = await sessions.authenticate_cookie(completed.grant.token, ("web",))
    assert authenticated.require_user().id == completed.user.id


async def test_completion_needs_a_live_setup_session(
    setup_service: SetupService, engine: AsyncEngine, sessions: SessionService
) -> None:
    await configure(setup_service, engine, sessions)
    claim = await setup_service.claim(await code_of(setup_service), client_key="10.0.0.1")
    await sessions.revoke(claim.session.id, "logout")

    with pytest.raises(ProblemError) as caught:
        await setup_service.complete_setup(claim.session, media_user())

    assert (caught.value.status, caught.value.code) == (403, "setup_session_required")
    async with engine.connect() as connection:
        assert not (await state_repository.read(connection)).setup_completed


async def test_completion_refuses_a_web_session(
    setup_service: SetupService, engine: AsyncEngine, sessions: SessionService
) -> None:
    await configure(setup_service, engine, sessions)
    grant = await open_setup_session(engine, sessions)
    web_like = replace(grant.session, kind="web")
    with pytest.raises(ProblemError) as caught:
        await setup_service.complete_setup(web_like, media_user())
    assert caught.value.code == "setup_session_required"


async def test_completion_refuses_a_user_who_is_not_an_administrator(
    setup_service: SetupService,
    engine: AsyncEngine,
    sessions: SessionService,
) -> None:
    await configure(setup_service, engine, sessions)
    claim = await setup_service.claim(await code_of(setup_service), client_key="10.0.0.1")

    with pytest.raises(ProblemError) as caught:
        await setup_service.complete_setup(claim.session, media_user(admin=False))

    assert (caught.value.status, caught.value.code) == (403, "admin_required")
    async with engine.connect() as connection:
        assert not (await state_repository.read(connection)).setup_completed


async def test_completion_needs_a_configured_media_server(
    setup_service: SetupService,
) -> None:
    claim = await setup_service.claim(await code_of(setup_service), client_key="10.0.0.1")
    with pytest.raises(ProblemError) as caught:
        await setup_service.complete_setup(claim.session, media_user())
    assert (caught.value.status, caught.value.code) == (503, "setup_required")


async def test_completion_applies_the_remote_access_rule(
    setup_service: SetupService,
    engine: AsyncEngine,
    sessions: SessionService,
) -> None:
    await configure(setup_service, engine, sessions)
    claim = await setup_service.claim(await code_of(setup_service), client_key="203.0.113.7")

    with pytest.raises(ProblemError) as caught:
        await setup_service.complete_setup(
            claim.session, media_user(remote_access=False), client_is_private=False
        )

    assert (caught.value.status, caught.value.code) == (403, "remote_access_denied")
    async with engine.connect() as connection:
        assert not (await state_repository.read(connection)).setup_completed


async def test_setup_cannot_be_completed_twice(
    setup_service: SetupService,
    engine: AsyncEngine,
    sessions: SessionService,
) -> None:
    await configure(setup_service, engine, sessions)
    code = await code_of(setup_service)
    first = await setup_service.claim(code, client_key="10.0.0.1")
    await setup_service.complete_setup(first.session, media_user())

    with pytest.raises(ProblemError) as caught:
        await setup_service.complete_setup(first.session, media_user())

    assert caught.value.code == "setup_session_required"
    with pytest.raises(ProblemError) as claimed:
        await setup_service.claim(code, client_key="10.0.0.1")
    assert claimed.value.code == "setup_completed"


# --- the reset ----------------------------------------------------------------------


async def test_the_reset_clears_the_media_server_and_starts_setup_again(
    setup_service: SetupService,
    engine: AsyncEngine,
    sessions: SessionService,
    settings_store: SettingsStore,
) -> None:
    await configure(setup_service, engine, sessions)
    first_code = await code_of(setup_service)
    claim = await setup_service.claim(first_code, client_key="10.0.0.1")
    completed = await setup_service.complete_setup(claim.session, media_user())

    path = await setup_service.reset()

    assert path == setup_service.code_path
    code = read_setup_code(path)
    assert code is not None
    assert code != first_code
    # Every session is gone and every user unlinked, but their rows stay.
    with pytest.raises(ProblemError):
        await sessions.authenticate_cookie(completed.grant.token, ("web",))
    async with engine.connect() as connection:
        state = await state_repository.read(connection)
        users = await users_repository.list_all(connection)
    assert state.setup_completed_at is None
    assert state.media_server_identity is None
    assert state.setup_code_hash == setup_code_hash(code)
    assert [user.disabled_reason for user in users] == ["unlinked"]
    assert (await settings_store.get("media_server_url")).value is None
    assert (await settings_store.get("media_server_api_key")).value is None
    # The new code works, and the wizard starts from an empty media server step.
    assert await setup_service.claim(code, client_key="10.0.0.1")
    assert (await setup_service.state(client_is_private=True)).media_server is None


async def test_the_reset_keeps_what_the_environment_forces(  # noqa: PLR0913, PLR0917
    engine: AsyncEngine,
    data_dir: Path,
    clock: FakeClock,
    sessions: SessionService,
    keys: KeyMaterial,
    media_servers: FakeMediaServers,
    limits: RateLimits,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = SettingsStore(
        engine,
        SecretCipher.for_settings(keys),
        {"media_server_url": "http://emby.lan:8096"},
    )
    connector = MediaServerConnector(engine, settings, media_servers)
    service = SetupService(engine, data_dir, clock, sessions, connector, settings, limits)

    with caplog.at_level(logging.WARNING):
        await service.reset()

    assert (await settings.get("media_server_url")).value == "http://emby.lan:8096"
    assert "forced by the environment" in caplog.text
