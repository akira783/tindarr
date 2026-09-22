"""The plex.tv client and the Plex media server adapter, against the fake plex.tv."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest

from tests.support.upstream import MACHINE_ID, FakePlexTv
from tindeerr.adapters.plex import PlexServer
from tindeerr.adapters.plextv import DEFAULT_BACKOFF_MS, PlexTvClient
from tindeerr.core.errors import ProblemError, RateLimitedError
from tindeerr.ports.media_server import MediaServerConnection
from tindeerr.ports.plextv import PlexAccount, PlexPin, PlexResource, as_media_user, find_server

pytestmark = pytest.mark.anyio

OWNER_TOKEN = "owner-token"
OWNER_ID = "42"


@pytest.fixture
def plex_tv() -> FakePlexTv:
    fake = FakePlexTv()
    fake.add_account(OWNER_TOKEN, OWNER_ID, "Alex")
    return fake


def client(plex_tv: FakePlexTv) -> PlexTvClient:
    return PlexTvClient(plex_tv.transport, base_url="https://plex.tv")


def plex_server(
    plex_tv: FakePlexTv, media_transport: httpx2.MockTransport, **fields: Any
) -> PlexServer:
    values: dict[str, Any] = {
        "kind": "plex",
        "url": "http://plex.lan:32400",
        "secret": OWNER_TOKEN,
        "device_id": "install-abc",
    }
    return PlexServer(MediaServerConnection(**(values | fields)), client(plex_tv), media_transport)


def plex_media_server(
    *, machine_id: str = MACHINE_ID, status: int = 200, offline: bool = False
) -> httpx2.MockTransport:
    """A Plex Media Server answering ``/identity`` and ``/``."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if offline:
            raise httpx2.ConnectError("down")
        if request.url.path == "/identity":
            return httpx2.Response(
                status,
                json={"MediaContainer": {"machineIdentifier": machine_id, "version": "1.41.0"}},
            )
        if request.url.path == "/":
            has_token = request.headers.get("x-plex-token") == OWNER_TOKEN
            return httpx2.Response(200 if has_token else 401, json={"MediaContainer": {}})
        return httpx2.Response(404, json={})

    return httpx2.MockTransport(handle)


# --- PINs ------------------------------------------------------------------------------


async def test_a_pin_is_created_with_the_client_identifier_and_device_name(
    plex_tv: FakePlexTv,
) -> None:
    pin = await client(plex_tv).create_pin("client-1", "Tindeerr (Home)")
    assert (pin.client_id, pin.code) == ("client-1", "CODE1")
    created = plex_tv.requests[-1]
    assert created.url.params["strong"] == "true"
    assert created.headers["x-plex-client-identifier"] == "client-1"
    assert created.headers["x-plex-device-name"] == "Tindeerr (Home)"
    assert created.headers["x-plex-product"] == "Tindeerr"
    assert pin.expires_at > datetime.now(UTC)


async def test_the_approval_link_names_tindeerr(plex_tv: FakePlexTv) -> None:
    pin = await client(plex_tv).create_pin("client-1", "Tindeerr (Home)")
    link = client(plex_tv).auth_url(pin)
    assert link.startswith("https://app.plex.tv/auth#?clientID=client-1&code=CODE1")
    assert link.endswith("context%5Bdevice%5D%5Bproduct%5D=Tindeerr")


async def test_a_pin_carries_no_token_until_it_is_approved(plex_tv: FakePlexTv) -> None:
    plex_tv_client = client(plex_tv)
    pin = await plex_tv_client.create_pin("client-1", "Tindeerr")
    assert await plex_tv_client.check_pin(pin) is None
    plex_tv.approve(pin.code, "user-token")
    assert await plex_tv_client.check_pin(pin) == "user-token"


async def test_a_pin_is_polled_with_its_own_client_identifier(plex_tv: FakePlexTv) -> None:
    pin = await client(plex_tv).create_pin("client-1", "Tindeerr")
    other = PlexPin(id=pin.id, code=pin.code, client_id="someone-else", expires_at=pin.expires_at)
    assert await client(plex_tv).check_pin(other) is None


async def test_an_unknown_pin_is_simply_not_approved(plex_tv: FakePlexTv) -> None:
    pin = PlexPin(id="9999", code="X", client_id="c", expires_at=datetime.now(UTC))
    assert await client(plex_tv).check_pin(pin) is None


async def test_plex_tv_being_unreachable_is_reported(plex_tv: FakePlexTv) -> None:
    plex_tv.offline = True
    with pytest.raises(ProblemError) as caught:
        await client(plex_tv).create_pin("client-1", "Tindeerr")
    assert (caught.value.status, caught.value.code) == (503, "plex_tv_unreachable")


async def test_a_pin_without_an_id_is_unusable(plex_tv: FakePlexTv) -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json={"code": "ABCD"})

    with pytest.raises(ProblemError) as caught:
        await PlexTvClient(httpx2.MockTransport(handle)).create_pin("c", "Tindeerr")
    assert caught.value.code == "plex_tv_unreachable"


@pytest.mark.parametrize(
    "payload",
    [
        {"id": 1, "code": "A", "expiresIn": 900},
        {"id": 1, "code": "A"},
        {"id": 1, "code": "A", "expiresAt": "nonsense"},
    ],
)
async def test_the_pin_expiry_falls_back_when_plex_tv_gives_none(payload: dict[str, Any]) -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(201, json=payload)

    pin = await PlexTvClient(httpx2.MockTransport(handle)).create_pin("c", "Tindeerr")
    assert pin.expires_at > datetime.now(UTC)


async def test_a_naive_expiry_is_read_as_utc() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        moment = (datetime.now(UTC) + timedelta(minutes=9)).replace(tzinfo=None)
        return httpx2.Response(201, json={"id": 1, "code": "A", "expiresAt": moment.isoformat()})

    pin = await PlexTvClient(httpx2.MockTransport(handle)).create_pin("c", "Tindeerr")
    assert pin.expires_at.tzinfo is not None


async def test_plex_tv_asking_us_to_slow_down_is_a_rate_limit(plex_tv: FakePlexTv) -> None:
    plex_tv.rate_limited.add("/api/v2/pins")
    with pytest.raises(RateLimitedError) as caught:
        await client(plex_tv).create_pin("client-1", "Tindeerr")
    assert caught.value.retry_after_ms == DEFAULT_BACKOFF_MS


async def test_a_retry_after_header_sets_the_backoff() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={}, headers={"Retry-After": "3"})

    with pytest.raises(RateLimitedError) as caught:
        await PlexTvClient(httpx2.MockTransport(handle)).create_pin("c", "Tindeerr")
    assert caught.value.retry_after_ms == 3000


# --- account and resources ---------------------------------------------------------------


async def test_the_account_is_keyed_by_its_decimal_id(plex_tv: FakePlexTv) -> None:
    account = await client(plex_tv).account(OWNER_TOKEN, "client-1")
    assert (account.id, account.name) == (OWNER_ID, "Alex")
    assert plex_tv.requests[-1].headers["x-plex-token"] == OWNER_TOKEN
    assert b"token" not in plex_tv.requests[-1].url.query


async def test_an_unknown_token_has_no_account(plex_tv: FakePlexTv) -> None:
    with pytest.raises(ProblemError) as caught:
        await client(plex_tv).account("nope", "client-1")
    assert caught.value.code == "plex_tv_unreachable"


async def test_an_account_without_a_numeric_id_is_refused() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "abc", "title": "Alex"})

    with pytest.raises(ProblemError):
        await PlexTvClient(httpx2.MockTransport(handle)).account("t", "client-1")


async def test_an_account_falls_back_to_its_username() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": 7, "username": "robin"})

    account = await PlexTvClient(httpx2.MockTransport(handle)).account("t", "client-1")
    assert (account.id, account.name) == ("7", "robin")


async def test_resources_are_read_with_their_provides_list(plex_tv: FakePlexTv) -> None:
    plex_tv.resources[OWNER_TOKEN][0]["provides"] = "server,player"
    [resource] = await client(plex_tv).resources(OWNER_TOKEN, "client-1")
    assert resource.provides == ("server", "player")
    assert resource.is_server is True
    assert resource.owned is True


async def test_a_resource_without_an_identifier_is_dropped(plex_tv: FakePlexTv) -> None:
    plex_tv.resources[OWNER_TOKEN].append({"name": "broken"})
    assert len(await client(plex_tv).resources(OWNER_TOKEN, "client-1")) == 1


async def test_unreadable_resources_are_reported(plex_tv: FakePlexTv) -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"not": "a list"})

    with pytest.raises(ProblemError):
        await PlexTvClient(httpx2.MockTransport(handle)).resources("t", "client-1")


def test_the_configured_server_is_matched_by_identifier_only() -> None:
    resources = [
        PlexResource("other", "Someone else's Plex", owned=True, provides=("server",)),
        PlexResource(MACHINE_ID, "A player, not a server", owned=True, provides=("player",)),
    ]
    assert find_server(resources, MACHINE_ID) is None
    resources.append(PlexResource(MACHINE_ID, "Home", owned=False, provides=("server",)))
    found = find_server(resources, MACHINE_ID)
    assert found is not None
    assert found.owned is False


def test_a_plex_account_becomes_a_media_user() -> None:
    user = as_media_user(PlexAccount("0042", "Alex"), admin=True)
    assert (user.id, user.is_admin, user.remote_access) == ("42", True, True)


def test_an_account_id_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ProblemError) as caught:
        as_media_user(PlexAccount("alex", "Alex"), admin=False)
    assert caught.value.code == "plex_tv_unreachable"


# --- devices -------------------------------------------------------------------------------


async def test_the_sign_in_device_is_deleted(plex_tv: FakePlexTv) -> None:
    plex_tv.devices["client-1"] = "1007"
    assert await client(plex_tv).delete_device(OWNER_TOKEN, "client-1") is True
    assert plex_tv.devices == {}
    assert plex_tv.requests[-1].method == "DELETE"


async def test_deleting_an_unknown_device_reports_failure(plex_tv: FakePlexTv) -> None:
    assert await client(plex_tv).delete_device(OWNER_TOKEN, "client-1") is False


async def test_a_failing_device_deletion_is_only_logged(
    plex_tv: FakePlexTv, log_stream: Any
) -> None:
    plex_tv.devices["client-1"] = "1007"
    plex_tv.device_deletion_fails = True
    assert await client(plex_tv).delete_device(OWNER_TOKEN, "client-1") is False
    assert "refused to remove the device" in log_stream.getvalue()


async def test_device_deletion_never_raises_when_plex_tv_is_down(
    plex_tv: FakePlexTv, log_stream: Any
) -> None:
    plex_tv.offline = True
    assert await client(plex_tv).delete_device(OWNER_TOKEN, "client-1") is False
    assert "could not remove the plex.tv device" in log_stream.getvalue()


async def test_device_deletion_survives_a_rate_limit(plex_tv: FakePlexTv) -> None:
    plex_tv.rate_limited.add("/devices.xml")
    assert await client(plex_tv).delete_device(OWNER_TOKEN, "client-1") is False


async def test_an_unauthorized_device_list_reports_failure(plex_tv: FakePlexTv) -> None:
    assert await client(plex_tv).delete_device("nope", "client-1") is False


async def test_a_device_list_that_is_not_xml_is_reported() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<not xml")

    assert await PlexTvClient(httpx2.MockTransport(handle)).delete_device("t", "c") is False


# --- shared users ----------------------------------------------------------------------------


async def test_shared_users_only_lists_accounts_of_this_server(plex_tv: FakePlexTv) -> None:
    plex_tv.shared = [("7", "Robin", MACHINE_ID), ("8", "Sam", "another-machine")]
    accounts = await client(plex_tv).shared_users(OWNER_TOKEN, MACHINE_ID, "client-1")
    assert [(account.id, account.name) for account in accounts] == [("7", "Robin")]


async def test_shared_users_fails_when_plex_tv_refuses(plex_tv: FakePlexTv) -> None:
    with pytest.raises(ProblemError) as caught:
        await client(plex_tv).shared_users("nope", MACHINE_ID, "client-1")
    assert caught.value.code == "plex_tv_unreachable"


async def test_shared_users_fails_on_an_unreadable_body() -> None:
    def handle(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"<broken")

    with pytest.raises(ProblemError):
        await PlexTvClient(httpx2.MockTransport(handle)).shared_users("t", MACHINE_ID, "client-1")


# --- the Plex media server ---------------------------------------------------------------------


async def test_the_plex_server_identity_is_its_machine_identifier(plex_tv: FakePlexTv) -> None:
    identity = await plex_server(plex_tv, plex_media_server()).identify()
    assert identity.key == f"plex:{MACHINE_ID}"
    assert identity.version == "1.41.0"


async def test_a_plex_server_that_does_not_answer_is_unreachable(plex_tv: FakePlexTv) -> None:
    with pytest.raises(ProblemError) as caught:
        await plex_server(plex_tv, plex_media_server(offline=True)).identify()
    assert caught.value.code == "media_server_unreachable"


async def test_a_plex_server_without_a_machine_identifier_is_unreachable(
    plex_tv: FakePlexTv,
) -> None:
    with pytest.raises(ProblemError):
        await plex_server(plex_tv, plex_media_server(machine_id="")).identify()


async def test_an_identity_answered_flat_is_read_too(plex_tv: FakePlexTv) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/identity":
            return httpx2.Response(200, json={"machineIdentifier": MACHINE_ID})
        return httpx2.Response(200, json={})

    identity = await plex_server(plex_tv, httpx2.MockTransport(handle)).identify()
    assert identity.server_id == MACHINE_ID


async def test_a_plex_connector_owned_by_the_token_is_ok(plex_tv: FakePlexTv) -> None:
    check = await plex_server(plex_tv, plex_media_server()).test()
    assert check.health == "ok"


async def test_a_plex_token_that_does_not_own_the_server_is_refused(
    plex_tv: FakePlexTv,
) -> None:
    plex_tv.add_account(OWNER_TOKEN, OWNER_ID, "Alex", owned=False)
    with pytest.raises(ProblemError) as caught:
        await plex_server(plex_tv, plex_media_server()).test()
    assert (caught.value.status, caught.value.code) == (403, "plex_owner_required")


async def test_a_plex_token_for_another_server_is_refused(plex_tv: FakePlexTv) -> None:
    plex_tv.add_account(OWNER_TOKEN, OWNER_ID, "Alex", machine_id="another")
    with pytest.raises(ProblemError) as caught:
        await plex_server(plex_tv, plex_media_server()).test()
    assert caught.value.code == "plex_owner_required"


async def test_a_plex_test_reports_an_unreachable_server(plex_tv: FakePlexTv) -> None:
    check = await plex_server(plex_tv, plex_media_server(offline=True)).test()
    assert check.health == "unreachable"


async def test_a_plex_test_reports_an_unreachable_plex_tv(plex_tv: FakePlexTv) -> None:
    plex_tv.offline = True
    assert (await plex_server(plex_tv, plex_media_server()).test()).health == "unreachable"


async def test_a_plex_token_the_server_refuses_is_unauthorized(plex_tv: FakePlexTv) -> None:
    check = await plex_server(plex_tv, plex_media_server(), secret=OWNER_TOKEN).test()
    assert check.health == "ok"
    plex_tv.add_account("other-token", OWNER_ID, "Alex")
    check = await plex_server(plex_tv, plex_media_server(), secret="other-token").test()
    assert check.health == "unauthorized"


async def test_a_plex_server_answering_oddly_at_its_root_is_unexpected(
    plex_tv: FakePlexTv,
) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/identity":
            return httpx2.Response(200, json={"MediaContainer": {"machineIdentifier": MACHINE_ID}})
        return httpx2.Response(500, json={})

    check = await plex_server(plex_tv, httpx2.MockTransport(handle)).test()
    assert check.health == "unexpected_response"


async def test_plex_has_no_password_and_no_quick_connect(plex_tv: FakePlexTv) -> None:
    adapter = plex_server(plex_tv, plex_media_server())
    assert await adapter.quick_connect_enabled() is False
    with pytest.raises(ProblemError) as caught:
        await adapter.authenticate_password("alex", "secret")
    assert (caught.value.status, caught.value.code) == (409, "sign_in_method_unavailable")
    for call in (adapter.quick_connect_start(), adapter.quick_connect_poll("x")):
        with pytest.raises(ProblemError) as refused:
            await call
        assert refused.value.code == "quick_connect_unavailable"


async def test_plex_users_are_the_owner_and_the_shared_accounts(plex_tv: FakePlexTv) -> None:
    plex_tv.shared = [("7", "Robin", MACHINE_ID), (OWNER_ID, "Alex", MACHINE_ID)]
    users = await plex_server(plex_tv, plex_media_server()).list_users()
    assert [(user.id, user.is_admin) for user in users] == [(OWNER_ID, True), ("7", False)]


async def test_a_rate_limited_sync_changes_nothing(plex_tv: FakePlexTv) -> None:
    plex_tv.rate_limited.add("/api/users")
    with pytest.raises(ProblemError) as caught:
        await plex_server(plex_tv, plex_media_server()).list_users()
    assert caught.value.code == "plex_tv_unreachable"


async def test_every_plex_tv_call_identifies_the_client(plex_tv: FakePlexTv) -> None:
    # plex.tv answers 400 to its v2 endpoints without X-Plex-Client-Identifier.
    await client(plex_tv).account(OWNER_TOKEN, "client-1")
    await client(plex_tv).resources(OWNER_TOKEN, "client-1")
    identified = [request.headers.get("x-plex-client-identifier") for request in plex_tv.requests]
    assert identified == ["client-1", "client-1"]


async def test_the_owner_token_calls_use_the_installs_identifier(plex_tv: FakePlexTv) -> None:
    adapter = plex_server(plex_tv, plex_media_server())
    plex_tv.shared = [("7", "Robin", MACHINE_ID)]
    await adapter.list_users()
    identifiers = {request.headers.get("x-plex-client-identifier") for request in plex_tv.requests}
    assert identifiers == {"install-abc"}
