"""Signing in: the password flows, first-run completion, roles and remote access.

Everything here runs the real adapters against the fake Jellyfin of
``tests.support.upstream``, so the assertions are about what the media server was
actually asked, not about a stub that agreed with us.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    ADMIN_API_KEY,
    ADMIN_NAME,
    ADMIN_PASSWORD,
    API,
    APP_DEVICE,
    MEDIA_SERVER_URL,
    USER_NAME,
    USER_PASSWORD,
    FakeClock,
    FakeInternet,
    app_login,
    build_app,
    claim,
    console_client,
    console_headers,
    run,
    seed_jellyfin,
    set_up_server,
    web_login,
)
from tests.support.flows import configure_media_server
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.api.cookies import SECURE_NAMES

PUBLIC_PEER = "203.0.113.5"


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet)


def sign_in_attempts(internet: FakeInternet) -> int:
    """How many password attempts actually reached the media server."""
    return sum(1 for r in internet.media.requests if r.url.path == "/Users/AuthenticateByName")


def set_password_sign_in(client: TestClient, app: FastAPI, value: str) -> None:
    """Change the ``password_sign_in`` setting inside the running application."""
    services: Any = app.state.services
    run(client, lambda: services.settings.set("password_sign_in", value))


def session_of(client: TestClient, app: FastAPI) -> Any:
    """The web session row behind the client's cookie."""
    services: Any = app.state.services
    token = client.cookies[SECURE_NAMES.session]
    return run(client, lambda: services.sessions.authenticate_cookie(token, ("web",)))


# --- first-run completion --------------------------------------------------------------


def test_the_first_administrator_completes_setup(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        response = web_login(client)
        body = response.json()
        assert_matches_contract(f"{API}/auth/web/login", "post", response)
        assert body["setup_completed_now"] is True
        assert body["user"]["role"] == "admin"
        assert body["user"]["media_server_admin"] is True
        # A new session, never the setup one: no fixation is possible.
        assert body["csrf_token"] != csrf
        assert body["kind"] == "web"
        assert client.cookies.get(SECURE_NAMES.session) is not None
        assert client.cookies.get(SECURE_NAMES.setup) is None
        # The media server session Tindarr opened to check the password was closed.
        assert internet.media.logouts == [f"user-token-{ADMIN_NAME}"]


def test_setup_needs_the_browser_that_claimed(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        del client.cookies[SECURE_NAMES.setup]
        response = web_login(client)
    assert_is_problem(response, 403, "setup_session_required")


def test_setup_is_not_completed_by_someone_who_is_not_an_administrator(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        response = web_login(client, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 403, "admin_required")


def test_the_app_cannot_sign_in_before_setup_is_done(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        response = app_login(client)
    assert_is_problem(response, 503, "setup_required")
    assert_matches_contract(f"{API}/auth/login", "post", response)


def test_a_web_sign_in_needs_a_configured_media_server(app: FastAPI) -> None:
    with console_client(app) as client:
        claim(client, app)
        response = web_login(client)
    assert_is_problem(response, 503, "setup_required")


# --- the app's password sign-in ---------------------------------------------------------


def test_the_app_gets_a_token_pair(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = app_login(client, USER_NAME, USER_PASSWORD)
    assert_matches_contract(f"{API}/auth/login", "post", response)
    body = response.json()
    assert body["user"]["name"] == USER_NAME
    assert body["user"]["role"] == "user"
    assert body["setup_completed_now"] is False
    assert body["access_token"]
    assert body["refresh_token"]
    assert internet.media.logouts[-1] == f"user-token-{USER_NAME}"


def test_the_token_pair_works_on_a_shared_endpoint(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        tokens = app_login(client, USER_NAME, USER_PASSWORD).json()
        client.cookies.clear()
        response = client.post(
            f"{API}/auth/logout",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )
    assert response.status_code == 204


@pytest.mark.parametrize(
    ("username", "password"),
    [(ADMIN_NAME, "wrong"), ("nobody at all", ADMIN_PASSWORD)],
)
def test_an_unknown_user_and_a_wrong_password_answer_the_same(
    app: FastAPI, username: str, password: str
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = app_login(client, username, password)
    assert_is_problem(response, 401, "invalid_credentials")
    assert response.json()["detail"] == "Wrong user name or password."


def test_a_user_disabled_on_the_media_server_is_refused(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.forbidden_users.add(USER_NAME)
        response = app_login(client, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 403, "account_disabled")


def test_too_many_active_sessions_reads_as_a_disabled_account(
    app: FastAPI, internet: FakeInternet
) -> None:
    # Jellyfin answers 403 for MaxActiveSessions too; the user must be told to act.
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.fails["/Users/AuthenticateByName"] = 403
        response = app_login(client, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 403, "account_disabled")


def test_an_unreachable_media_server_is_a_503(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.offline = True
        response = app_login(client)
    assert_is_problem(response, 503, "media_server_unreachable")


# --- the per-username cap and the per-address pause ----------------------------------------


def test_at_most_two_failures_per_username_reach_the_media_server(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        before = sign_in_attempts(internet)
        for _ in range(2):
            assert app_login(client, USER_NAME, "wrong").status_code == 401
        response = app_login(client, USER_NAME, "wrong")
        assert_is_problem(response, 429, "rate_limited")
        assert response.json()["retry_after_ms"] > 0
        assert response.headers["Retry-After"]
        # The third attempt never left Tindarr: that is what protects the lockout.
        assert sign_in_attempts(internet) - before == 2
        # It is a pause, not a lock: the window empties on its own.
        clock.advance(15 * 60 + 1)
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200


def test_the_cap_is_the_same_for_a_username_that_does_not_exist(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        before = sign_in_attempts(internet)
        for _ in range(2):
            assert app_login(client, "ghost", "wrong").status_code == 401
        assert_is_problem(app_login(client, "ghost", "wrong"), 429, "rate_limited")
    assert sign_in_attempts(internet) - before == 2


def test_the_cap_folds_the_case_of_the_username(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, "wrong").status_code == 401
        assert app_login(client, USER_NAME.upper(), "wrong").status_code == 401
        assert_is_problem(
            app_login(client, f"{USER_NAME[0].upper()}{USER_NAME[1:]}", "x"), 429, "rate_limited"
        )


def test_a_success_clears_the_username_count(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, "wrong").status_code == 401
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        # The one earlier failure was forgotten, so two more still get through.
        assert app_login(client, USER_NAME, "wrong").status_code == 401
        assert app_login(client, USER_NAME, "wrong").status_code == 401


def test_failures_from_one_address_are_slowed_down(
    app: FastAPI, clock: FakeClock, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        for index in range(6):
            # A different username each time, so only the per-address limit applies.
            internet.media.add_user(f"user{index}", "pw", user_id=f"{index}" * 32, admin=False)
            assert app_login(client, f"user{index}", "wrong").status_code == 401
    assert clock.slept, "the sixth failure should have been paused"
    assert clock.slept[0] == pytest.approx(1.0)


# --- the password_sign_in setting ----------------------------------------------------------


def test_passwords_can_be_switched_off(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        set_password_sign_in(client, app, "disabled")
        response = app_login(client, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 403, "password_sign_in_disabled")


def test_passwords_can_be_limited_to_the_local_network(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        set_password_sign_in(client, app, "lan_only")
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
    with console_client(app, peer=PUBLIC_PEER) as outside:
        response = app_login(outside, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 403, "password_sign_in_disabled")


def test_server_info_leaves_password_out_when_it_does_not_apply(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        set_password_sign_in(client, app, "lan_only")
        assert "password" in client.get(f"{API}/server/info").json()["auth_methods"]
    with console_client(app, peer=PUBLIC_PEER) as outside:
        info = outside.get(f"{API}/server/info").json()
    assert info["auth_methods"] == []


# --- the remote-access rule ------------------------------------------------------------------


def test_a_user_without_remote_access_is_refused_from_outside(
    app: FastAPI, internet: FakeInternet
) -> None:
    internet.media.add_user("sam", "pw", user_id="c" * 32, admin=False, remote_access=False)
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, "sam", "pw").status_code == 200
    with console_client(app, peer=PUBLIC_PEER) as outside:
        response = app_login(outside, "sam", "pw")
    assert_is_problem(response, 403, "remote_access_denied")


def test_the_rule_applies_to_every_later_request_too(app: FastAPI, internet: FakeInternet) -> None:
    internet.media.add_user("sam", "pw", user_id="c" * 32, admin=False, remote_access=False)
    with console_client(app) as client:
        set_up_server(client, app)
        tokens = app_login(client, "sam", "pw").json()
    with console_client(app, peer=PUBLIC_PEER) as outside:
        response = outside.post(
            f"{API}/auth/logout", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
    assert_is_problem(response, 403, "remote_access_denied")


def test_a_refresh_from_outside_is_refused_too(app: FastAPI, internet: FakeInternet) -> None:
    internet.media.add_user("sam", "pw", user_id="c" * 32, admin=False, remote_access=False)
    with console_client(app) as client:
        set_up_server(client, app)
        tokens = app_login(client, "sam", "pw").json()
    with console_client(app, peer=PUBLIC_PEER) as outside:
        response = outside.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
    assert_is_problem(response, 403, "remote_access_denied")


# --- the administrator flag follows the media server ------------------------------------------


def test_a_promotion_on_the_media_server_is_picked_up_at_the_next_sign_in(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, USER_PASSWORD).json()["user"]["role"] == "user"
        internet.media.users[USER_NAME]["Policy"]["IsAdministrator"] = True
        body = app_login(client, USER_NAME, USER_PASSWORD).json()
    assert body["user"]["role"] == "admin"
    assert body["user"]["media_server_admin"] is True


def test_a_demotion_on_the_media_server_is_picked_up_at_the_next_sign_in(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.users[ADMIN_NAME]["Policy"]["IsAdministrator"] = False
        body = app_login(client).json()
    assert body["user"]["media_server_admin"] is False


# --- console rules -----------------------------------------------------------------------------


def test_a_bearer_token_never_signs_the_console_in(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.post(
            f"{API}/auth/web/login",
            json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD},
            headers=console_headers() | {"Authorization": "Bearer something"},
        )
    assert_is_problem(response, 401, "unauthorized")


def test_a_web_sign_in_checks_the_origin(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.post(
            f"{API}/auth/web/login", json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD}
        )
    assert_is_problem(response, 403, "csrf_failed")


def test_a_web_sign_in_over_plain_http_is_refused(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    plain = build_app(data_dir, clock=clock, internet=internet)
    with console_client(plain, scheme="http") as client:
        response = client.post(
            f"{API}/auth/web/login",
            json={"username": ADMIN_NAME, "password": ADMIN_PASSWORD},
            headers={"Origin": "http://console.test"},
        )
    assert_is_problem(response, 403, "https_required")


def test_a_web_session_names_the_browser_it_belongs_to(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        session = session_of(client, app)
    assert session.session.device.platform == "web"
    assert session.session.device.name is not None


def test_the_console_sign_in_counts_as_a_fresh_reauthentication(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        session = client.get(f"{API}/auth/web/session").json()
    assert session["reauth_expires_at"] is not None


def test_the_admin_api_key_is_never_sent_to_a_sign_in(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        app_login(client, USER_NAME, USER_PASSWORD)
    sign_in = next(r for r in internet.media.requests if r.url.path == "/Users/AuthenticateByName")
    assert ADMIN_API_KEY not in sign_in.headers["authorization"]
    assert str(sign_in.url).startswith(MEDIA_SERVER_URL)


def test_the_app_device_is_recorded_on_the_session(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        tokens = app_login(client, USER_NAME, USER_PASSWORD).json()
        services: Any = app.state.services
        token: str = tokens["access_token"]
        authenticated = run(client, lambda: services.sessions.authenticate_access_token(token))
    device = authenticated.session.device
    assert (device.name, device.platform, device.app_version) == (
        APP_DEVICE["name"],
        APP_DEVICE["platform"],
        APP_DEVICE["app_version"],
    )
