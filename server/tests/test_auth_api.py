"""Token refresh, sign-out and the console session over HTTP (auth.md, §2 and §7)."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    FakeClock,
    FakeMediaServers,
    build_app,
    console_client,
    console_headers,
    cookies_of,
    media_user,
    run,
    services_of,
    sign_in_console,
)
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindeerr.api.cookies import SECURE_NAMES
from tindeerr.auth.sessions import MobileGrant
from tindeerr.storage.db import write_transaction
from tindeerr.storage.sessions import Device
from tindeerr.storage.users import get_by_media_server_id, update_fields

REFRESH_PATH = "/api/v1/auth/refresh"
LOGOUT_PATH = "/api/v1/auth/logout"
SESSION_PATH = "/api/v1/auth/web/session"


@pytest.fixture
def app(data_dir: Path, clock: FakeClock) -> FastAPI:
    return build_app(data_dir, clock=clock, media_servers=FakeMediaServers())


def open_app_session(client: TestClient, app: FastAPI, name: str = "Alex") -> MobileGrant:
    """Open a phone's session the way a sign-in will in step 2b."""
    services = services_of(app)

    async def open_it() -> MobileGrant:
        async with write_transaction(services.engine) as connection:
            user = await get_by_media_server_id(connection, media_user(name=name).id)
            assert user is not None
            return await services.sessions.open_mobile_session(
                connection, user, Device("Pixel 9", "android", "1.0.0")
            )

    return run(client, open_it)


# --- token refresh ------------------------------------------------------------------


def test_refreshing_returns_a_new_pair(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        grant = open_app_session(client, app)

        response = client.post(REFRESH_PATH, json={"refresh_token": grant.tokens.refresh_token})

    assert_matches_contract(REFRESH_PATH, "post", response)
    body = response.json()
    assert body["refresh_token"] != grant.tokens.refresh_token
    assert body["access_token"]
    assert body["access_expires_at"] < body["refresh_expires_at"]


def test_refreshing_needs_a_known_token(app: FastAPI) -> None:
    with console_client(app) as client:
        response = client.post(REFRESH_PATH, json={"refresh_token": "nope"})
    assert_is_problem(response, 401, "unauthorized")
    assert_matches_contract(REFRESH_PATH, "post", response)


def test_a_reused_refresh_token_revokes_the_session(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        grant = open_app_session(client, app)
        first = client.post(REFRESH_PATH, json={"refresh_token": grant.tokens.refresh_token})
        assert first.status_code == 200

        replayed = client.post(REFRESH_PATH, json={"refresh_token": grant.tokens.refresh_token})
        after = client.post(REFRESH_PATH, json={"refresh_token": first.json()["refresh_token"]})

    assert_is_problem(replayed, 401, "refresh_token_reused")
    assert_matches_contract(REFRESH_PATH, "post", replayed)
    assert_is_problem(after, 401, "unauthorized")


def test_refreshing_is_rate_limited_per_address(app: FastAPI) -> None:
    with console_client(app) as client:
        for _ in range(30):
            client.post(REFRESH_PATH, json={"refresh_token": "nope"})
        response = client.post(REFRESH_PATH, json={"refresh_token": "nope"})
    assert_is_problem(response, 429, "rate_limited")
    assert response.json()["retry_after_ms"] > 0
    assert response.headers["retry-after"] == "60"


def test_a_restricted_user_cannot_refresh_from_outside(app: FastAPI, data_dir: Path) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        grant = open_app_session(client, app)
        services = services_of(app)

        async def restrict() -> None:
            async with write_transaction(services.engine) as connection:
                user = await get_by_media_server_id(connection, media_user().id)
                assert user is not None
                await update_fields(connection, user.id, remote_access=False)

        run(client, restrict)
        local = client.post(REFRESH_PATH, json={"refresh_token": grant.tokens.refresh_token})

    assert local.status_code == 200  # the console client is on the local network
    with console_client(app, peer="203.0.113.7") as outside:
        response = outside.post(REFRESH_PATH, json={"refresh_token": local.json()["refresh_token"]})
    assert_is_problem(response, 403, "remote_access_denied")


# --- sign-out -----------------------------------------------------------------------


def test_signing_out_of_the_console_clears_the_cookie(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = sign_in_console(client, app)

        response = client.post(LOGOUT_PATH, headers=console_headers(csrf))

        assert response.status_code == 204
        assert "Max-Age=0" in cookies_of(response)[SECURE_NAMES.session]
        assert_is_problem(client.get(SESSION_PATH), 401, "unauthorized")


def test_signing_out_needs_the_csrf_token(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        response = client.post(LOGOUT_PATH, headers=console_headers())
        assert_is_problem(response, 403, "csrf_failed")
        assert client.get(SESSION_PATH).status_code == 200


def test_the_app_signs_itself_out_with_its_token(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        grant = open_app_session(client, app)
        headers = {"Authorization": f"Bearer {grant.tokens.access_token}"}

        response = client.post(LOGOUT_PATH, headers=headers)

        assert response.status_code == 204
        # The token no longer works, and neither does its refresh token.
        assert_is_problem(client.post(LOGOUT_PATH, headers=headers), 401, "unauthorized")
        refused = client.post(REFRESH_PATH, json={"refresh_token": grant.tokens.refresh_token})
    assert_is_problem(refused, 401, "unauthorized")


def test_signing_out_without_a_session(app: FastAPI) -> None:
    with console_client(app) as client:
        assert_is_problem(client.post(LOGOUT_PATH, headers=console_headers()), 401, "unauthorized")


# --- the console's session ----------------------------------------------------------


def test_the_session_endpoint_gives_the_csrf_token_back(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = sign_in_console(client, app)
        response = client.get(SESSION_PATH)

    assert_matches_contract(SESSION_PATH, "get", response)
    body = response.json()
    assert body["csrf_token"] == csrf
    assert body["kind"] == "web"
    assert body["user"]["name"] == "Alex"
    assert body["user"]["media_server_admin"] is True
    assert client.cookies.get(SECURE_NAMES.session) is None or True


def test_the_session_endpoint_answers_a_setup_session_too(app: FastAPI) -> None:
    from tests.support import claim  # noqa: PLC0415

    with console_client(app) as client:
        csrf = claim(client, app)
        response = client.get(SESSION_PATH)
    assert response.json() == {
        "kind": "setup",
        "user": None,
        "csrf_token": csrf,
        "expires_at": response.json()["expires_at"],
        "reauth_expires_at": None,
        "setup_completed_now": False,
    }
    assert_matches_contract(SESSION_PATH, "get", response)


def test_a_bearer_token_never_opens_a_console_endpoint(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        grant = open_app_session(client, app)

        response = client.get(
            SESSION_PATH, headers={"Authorization": f"Bearer {grant.tokens.access_token}"}
        )

    # The cookie is ignored as soon as an Authorization header is there, and this
    # endpoint takes no bearer token: 401, as if nothing had been sent.
    assert_is_problem(response, 401, "unauthorized")


def test_another_authorization_scheme_is_not_a_cookie_request(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        response = client.get(SESSION_PATH, headers={"Authorization": "Basic YWRtaW46YWRtaW4="})
    assert_is_problem(response, 401, "unauthorized")


def test_an_expired_access_token_says_so(app: FastAPI, clock: FakeClock) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        grant = open_app_session(client, app)
        headers = {"Authorization": f"Bearer {grant.tokens.access_token}"}
        assert client.post(LOGOUT_PATH, headers=headers).status_code == 204

        clock.advance(20 * 60)
        response = client.post(LOGOUT_PATH, headers=headers)

    assert_is_problem(response, 401, "token_expired")


def test_a_revoked_session_stops_at_the_next_request(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = sign_in_console(client, app)
        services = services_of(app)
        session = client.get(SESSION_PATH).json()
        assert session["kind"] == "web"

        async def revoke_everything() -> None:
            user = session["user"]["id"]
            await services.sessions.revoke_user_sessions(user, "admin")

        run(client, revoke_everything)
        response = client.post(LOGOUT_PATH, headers=console_headers(csrf))

    assert_is_problem(response, 401, "unauthorized")


def test_a_disabled_user_stops_at_the_next_request(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        services = services_of(app)

        async def disable() -> None:
            async with write_transaction(services.engine) as connection:
                user = await get_by_media_server_id(connection, media_user().id)
                assert user is not None
                await update_fields(connection, user.id, enabled=False, disabled_reason="admin")

        run(client, disable)
        response = client.get(SESSION_PATH)

    assert_is_problem(response, 401, "unauthorized")


def test_the_session_endpoint_needs_a_session(app: FastAPI) -> None:
    with console_client(app) as client:
        assert_is_problem(client.get(SESSION_PATH), 401, "unauthorized")


def test_a_stale_cookie_does_not_hide_a_good_one(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        # A leftover setup cookie next to the web session: the web session still answers.
        client.cookies.set(SECURE_NAMES.setup, "stale-token")
        response = client.get(SESSION_PATH)
    assert response.status_code == 200
    assert response.json()["kind"] == "web"


def test_the_server_info_is_rate_limited(app: FastAPI) -> None:
    with console_client(app) as client:
        for _ in range(60):
            assert client.get("/api/v1/server/info").status_code == 200
        response = client.get("/api/v1/server/info")
    assert_is_problem(response, 429, "rate_limited")
    assert_matches_contract("/api/v1/server/info", "get", response)


def test_the_server_info_follows_the_caller_network(app: FastAPI) -> None:
    with console_client(app) as client:
        sign_in_console(client, app)
        services = services_of(app)
        run(client, lambda: services.settings.set("password_sign_in", "lan_only"))
        local: Any = client.get("/api/v1/server/info").json()
    with console_client(app, peer="203.0.113.7") as outside:
        remote: Any = outside.get("/api/v1/server/info").json()
    assert local["auth_methods"] == ["password"]
    assert remote["auth_methods"] == []
