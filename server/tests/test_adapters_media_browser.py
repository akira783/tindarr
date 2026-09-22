"""The Jellyfin and Emby adapters, against the fake servers of ``tests.support.upstream``."""

import logging
from typing import Any

import httpx2
import pytest

from tests.support.upstream import ADMIN_API_KEY, ADMIN_ID, SERVER_ID, FakeMediaBrowser
from tindarr.adapters.emby import EmbyServer
from tindarr.adapters.jellyfin import JellyfinServer
from tindarr.adapters.mediabrowser import (
    ELEVATION_PROBE_PATH,
    MediaBrowserServer,
    authorization_header,
    parse_version,
)
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import MediaServerConnection

pytestmark = pytest.mark.anyio

INSTALL_ID = "install-abc"


def connection(kind: str = "jellyfin", **fields: Any) -> MediaServerConnection:
    values: dict[str, Any] = {
        "kind": kind,
        "url": "http://media.lan:8096/",
        "secret": ADMIN_API_KEY,
        "device_id": INSTALL_ID,
    }
    return MediaServerConnection(**(values | fields))


def jellyfin(server: FakeMediaBrowser, **fields: Any) -> JellyfinServer:
    return JellyfinServer(connection(**fields), server.transport)


def emby(server: FakeMediaBrowser, **fields: Any) -> EmbyServer:
    return EmbyServer(connection("emby", **fields), server.transport)


@pytest.fixture
def server() -> FakeMediaBrowser:
    fake = FakeMediaBrowser()
    fake.add_user("alex", "secret", user_id=ADMIN_ID, admin=True)
    return fake


# --- the client identity -------------------------------------------------------------


def test_the_authorization_header_carries_the_four_client_fields() -> None:
    header = authorization_header("device-1", "token-1")
    assert header == (
        'MediaBrowser Client="Tindarr", Device="Tindarr server", '
        'DeviceId="device-1", Version="1", Token="token-1"'
    )


def test_the_authorization_header_leaves_the_token_out_when_there_is_none() -> None:
    assert "Token" not in authorization_header("device-1")


def test_the_authorization_header_falls_back_to_a_device_id() -> None:
    assert 'DeviceId="tindarr"' in authorization_header("")


async def test_every_call_identifies_tindarr_the_same_way(server: FakeMediaBrowser) -> None:
    await jellyfin(server).identify()
    header = server.requests[-1].headers["authorization"]
    assert header.startswith("MediaBrowser ")
    assert 'DeviceId="install-abc"' in header
    assert 'Version="1"' in header
    # The legacy forms Jellyfin 12 ignores are never sent.
    assert "x-emby-token" not in server.requests[-1].headers
    assert "x-emby-authorization" not in server.requests[-1].headers
    assert b"api_key" not in server.requests[-1].url.query


@pytest.mark.parametrize(
    ("text", "expected"),
    [("10.10.3", (10, 10, 3)), ("12.0.0-rc1", (12, 0, 0)), ("", ()), ("next", ())],
)
def test_versions_are_read_as_numbers(text: str, expected: tuple[int, ...]) -> None:
    assert parse_version(text) == expected


# --- identity and the connection test ------------------------------------------------


async def test_identify_reads_the_server_id_and_normalises_it(server: FakeMediaBrowser) -> None:
    server.server_id = "9F8E7D6C-5B4A-3928-1706-F5E4D3C2B1A0"
    identity = await jellyfin(server).identify()
    assert identity.key == f"jellyfin:{SERVER_ID}"
    assert identity.name == "Home Jellyfin"
    assert identity.version == "10.10.3"


async def test_identify_fails_when_the_server_does_not_answer(server: FakeMediaBrowser) -> None:
    server.offline = True
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).identify()
    assert (caught.value.status, caught.value.code) == (503, "media_server_unreachable")


async def test_identify_fails_without_a_server_id(server: FakeMediaBrowser) -> None:
    server.server_id = ""
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).identify()
    assert caught.value.code == "media_server_unreachable"


async def test_a_working_connector_is_ok(server: FakeMediaBrowser) -> None:
    check = await jellyfin(server).test()
    assert (check.health, check.server_name, check.server_version) == (
        "ok",
        "Home Jellyfin",
        "10.10.3",
    )


async def test_a_wrong_api_key_is_unauthorized(server: FakeMediaBrowser) -> None:
    check = await jellyfin(server, secret="wrong").test()
    assert check.health == "unauthorized"


async def test_an_unreachable_server_is_unreachable(server: FakeMediaBrowser) -> None:
    server.offline = True
    assert (await jellyfin(server).test()).health == "unreachable"


async def test_an_unreadable_public_info_is_an_unexpected_response(
    server: FakeMediaBrowser,
) -> None:
    server.fails["/System/Info/Public"] = 500
    assert (await jellyfin(server).test()).health == "unexpected_response"


async def test_a_failing_users_call_is_an_unexpected_response(server: FakeMediaBrowser) -> None:
    server.fails["/Users"] = 500
    assert (await jellyfin(server).test()).health == "unexpected_response"


async def test_a_user_token_is_not_an_admin_key(server: FakeMediaBrowser) -> None:
    # The whole point of probing an elevation-gated endpoint: GET /Users answers 200
    # to this token and filters the list, so it alone would let it through.
    server.add_user("robin", "pw", user_id="b" * 32, admin=False)
    token = server.issue_user_token("robin")
    check = await jellyfin(server, secret=token).test()
    assert check.health == "unauthorized"


async def test_a_server_without_the_elevation_endpoint_is_still_accepted(
    server: FakeMediaBrowser, caplog: pytest.LogCaptureFixture
) -> None:
    # An older or forked build: refusing would lock the operator out over a probe.
    server.fails["/System/Configuration"] = 404
    with caplog.at_level(logging.WARNING, logger="tindarr.adapters.mediabrowser"):
        assert (await jellyfin(server).test()).health == "ok"
    assert "administrator key" in caplog.text


async def test_the_elevation_probe_carries_the_api_key(server: FakeMediaBrowser) -> None:
    await jellyfin(server).test()
    probe = next(r for r in server.requests if r.url.path == ELEVATION_PROBE_PATH)
    assert 'Token="admin-api-key"' in probe.headers["authorization"]


async def test_jellyfin_older_than_10_10_is_unsupported(server: FakeMediaBrowser) -> None:
    server.version = "10.9.11"
    check = await jellyfin(server).test()
    assert (check.health, check.server_version) == ("unsupported_version", "10.9.11")


async def test_jellyfin_10_10_and_newer_are_supported(server: FakeMediaBrowser) -> None:
    for version in ("10.10.0", "10.11.2", "12.0.0"):
        server.version = version
        assert (await jellyfin(server).test()).health == "ok"


async def test_an_emby_answering_as_jellyfin_is_unsupported(server: FakeMediaBrowser) -> None:
    server.product_name = "Emby Server"
    assert (await jellyfin(server).test()).health == "unsupported_version"


async def test_a_jellyfin_answering_as_emby_is_unsupported(server: FakeMediaBrowser) -> None:
    server.kind = "emby"
    server.product_name = "Jellyfin Server"
    assert (await emby(server).test()).health == "unsupported_version"


async def test_an_emby_without_a_product_name_is_accepted(server: FakeMediaBrowser) -> None:
    server.kind = "emby"
    server.product_name = ""
    check = await emby(server).test()
    assert check.health == "ok"
    assert (await emby(server).identify()).kind == "emby"


# --- password sign-in -----------------------------------------------------------------


async def test_a_password_sign_in_returns_the_user_and_ends_the_session(
    server: FakeMediaBrowser,
) -> None:
    user = await jellyfin(server).authenticate_password("alex", "secret")
    assert (user.id, user.name, user.is_admin, user.remote_access) == (ADMIN_ID, "alex", True, True)
    assert server.logouts == ["user-token-alex"]


async def test_the_sign_in_call_carries_no_token(server: FakeMediaBrowser) -> None:
    await jellyfin(server).authenticate_password("alex", "secret")
    sign_in = next(r for r in server.requests if r.url.path == "/Users/AuthenticateByName")
    assert "Token=" not in sign_in.headers["authorization"]
    assert b'"Pw"' in sign_in.content


async def test_the_logout_uses_the_users_own_token(server: FakeMediaBrowser) -> None:
    await jellyfin(server).authenticate_password("alex", "secret")
    logout = next(r for r in server.requests if r.url.path == "/Sessions/Logout")
    assert 'Token="user-token-alex"' in logout.headers["authorization"]


@pytest.mark.parametrize(("name", "password"), [("alex", "wrong"), ("nobody", "secret")])
async def test_a_wrong_password_and_an_unknown_user_answer_the_same(
    server: FakeMediaBrowser, name: str, password: str
) -> None:
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).authenticate_password(name, password)
    assert (caught.value.status, caught.value.code, caught.value.detail) == (
        401,
        "invalid_credentials",
        "Wrong user name or password.",
    )


async def test_a_403_from_the_media_server_is_a_disabled_account(
    server: FakeMediaBrowser,
) -> None:
    server.forbidden_users.add("alex")
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).authenticate_password("alex", "secret")
    assert (caught.value.status, caught.value.code) == (403, "account_disabled")


async def test_a_broken_sign_in_response_is_unreachable(server: FakeMediaBrowser) -> None:
    server.users["alex"] = {"Name": "alex"}  # no Id: unusable
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).authenticate_password("alex", "secret")
    assert caught.value.code == "media_server_unreachable"


async def test_a_failed_logout_does_not_fail_the_sign_in(
    server: FakeMediaBrowser, log_stream: Any
) -> None:
    server.fails["/Sessions/Logout"] = 500
    user = await jellyfin(server).authenticate_password("alex", "secret")
    assert user.id == ADMIN_ID
    assert "refused to end the session" in log_stream.getvalue()


# --- listing users ---------------------------------------------------------------------


async def test_list_users_reads_every_policy_flag(server: FakeMediaBrowser) -> None:
    server.add_user("robin", "pw", user_id="b" * 32, admin=False, remote_access=False)
    users = {user.name: user for user in await jellyfin(server).list_users()}
    assert users["alex"].is_admin is True
    assert users["robin"].is_admin is False
    assert users["robin"].remote_access is False


async def test_list_users_skips_a_row_it_cannot_read(
    server: FakeMediaBrowser, log_stream: Any
) -> None:
    server.users["broken"] = {"Id": "not-a-guid", "Name": "broken"}
    assert [user.name for user in await jellyfin(server).list_users()] == ["alex"]
    assert "skipped a media server user" in log_stream.getvalue()


async def test_list_users_fails_when_the_server_does_not_answer(
    server: FakeMediaBrowser,
) -> None:
    server.fails["/Users"] = 500
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).list_users()
    assert caught.value.code == "media_server_unreachable"


async def test_a_missing_policy_denies_remote_access(server: FakeMediaBrowser) -> None:
    server.users["alex"] = {"Id": ADMIN_ID, "Name": "alex"}
    [user] = await jellyfin(server).list_users()
    assert (user.is_admin, user.remote_access) == (False, False)


# --- Quick Connect ----------------------------------------------------------------------


async def test_quick_connect_is_read_from_the_server(server: FakeMediaBrowser) -> None:
    assert await jellyfin(server).quick_connect_enabled() is True
    server.quick_connect_enabled = False
    assert await jellyfin(server).quick_connect_enabled() is False


async def test_quick_connect_is_off_when_the_endpoint_is_missing(
    server: FakeMediaBrowser,
) -> None:
    server.fails["/QuickConnect/Enabled"] = 404
    assert await jellyfin(server).quick_connect_enabled() is False


async def test_quick_connect_probe_fails_when_the_server_is_down(
    server: FakeMediaBrowser,
) -> None:
    server.offline = True
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_enabled()
    assert caught.value.code == "media_server_unreachable"


async def test_quick_connect_start_asks_with_a_post(server: FakeMediaBrowser) -> None:
    started = await jellyfin(server).quick_connect_start()
    assert started.code == "123451"
    initiate = next(r for r in server.requests if r.url.path == "/QuickConnect/Initiate")
    assert initiate.method == "POST"


async def test_quick_connect_start_refuses_when_it_is_disabled(
    server: FakeMediaBrowser,
) -> None:
    server.quick_connect_enabled = False
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_start()
    assert (caught.value.status, caught.value.code) == (409, "quick_connect_unavailable")


async def test_quick_connect_start_refuses_a_response_without_a_secret(
    server: FakeMediaBrowser,
) -> None:
    server.fails["/QuickConnect/Initiate"] = 200
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_start()
    assert caught.value.code == "media_server_unreachable"


async def test_quick_connect_poll_waits_until_the_code_is_approved(
    server: FakeMediaBrowser,
) -> None:
    secret = server.start_quick_connect()
    adapter = jellyfin(server)
    assert await adapter.quick_connect_poll(secret) is None
    server.approve(secret, "alex")
    user = await adapter.quick_connect_poll(secret)
    assert user is not None
    assert user.id == ADMIN_ID
    # The session the approval created is ended straight away.
    assert server.logouts == ["user-token-alex"]


async def test_quick_connect_poll_sends_the_secret_in_the_query_jellyfin_expects(
    server: FakeMediaBrowser,
) -> None:
    secret = server.start_quick_connect()
    await jellyfin(server).quick_connect_poll(secret)
    connect = next(r for r in server.requests if r.url.path == "/QuickConnect/Connect")
    assert connect.url.query == f"secret={secret}".encode()


async def test_an_unknown_quick_connect_secret_is_expired(server: FakeMediaBrowser) -> None:
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_poll("gone")
    assert (caught.value.status, caught.value.code) == (410, "quick_connect_expired")


async def test_a_quick_connect_approval_that_went_stale_is_expired(
    server: FakeMediaBrowser,
) -> None:
    secret = server.start_quick_connect()
    server.approve(secret, "alex")
    server.fails["/Users/AuthenticateWithQuickConnect"] = 401
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_poll(secret)
    assert caught.value.code == "quick_connect_expired"


async def test_a_disabled_user_approving_quick_connect_is_refused(
    server: FakeMediaBrowser,
) -> None:
    secret = server.start_quick_connect()
    server.approve(secret, "alex")
    server.forbidden_users.add("alex")
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_poll(secret)
    assert (caught.value.status, caught.value.code) == (403, "account_disabled")


async def test_a_quick_connect_poll_on_an_unreachable_server_fails(
    server: FakeMediaBrowser,
) -> None:
    server.offline = True
    with pytest.raises(ProblemError) as caught:
        await jellyfin(server).quick_connect_poll("whatever")
    assert caught.value.code == "media_server_unreachable"


# --- Emby has no Quick Connect ----------------------------------------------------------


async def test_emby_has_no_quick_connect(server: FakeMediaBrowser) -> None:
    server.kind = "emby"
    adapter = emby(server)
    assert await adapter.quick_connect_enabled() is False
    for call in (adapter.quick_connect_start(), adapter.quick_connect_poll("x")):
        with pytest.raises(ProblemError) as caught:
            await call
        assert (caught.value.status, caught.value.code) == (409, "quick_connect_unavailable")


async def test_emby_signs_in_with_a_password_like_jellyfin(server: FakeMediaBrowser) -> None:
    server.kind = "emby"
    user = await emby(server).authenticate_password("alex", "secret")
    assert user.id == ADMIN_ID


def test_the_base_class_is_never_used_on_its_own() -> None:
    # ``kind`` is declared but not set: only the two subclasses are complete.
    assert "kind" not in vars(MediaBrowserServer)


async def test_a_timeout_reads_as_unreachable() -> None:
    def time_out(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow")

    adapter = JellyfinServer(connection(), httpx2.MockTransport(time_out))
    assert (await adapter.test()).health == "unreachable"
