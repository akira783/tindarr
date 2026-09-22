"""The caller's own account and devices (`/me`), from both clients.

Reading is shared; revoking a session and deleting the account's data are console only,
which is the point of the `console` tag: a token stolen from a phone must not be able
to sign the owner's browser out or erase their account.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    API,
    APP_DEVICE,
    USER_NAME,
    USER_PASSWORD,
    FakeClock,
    FakeInternet,
    app_login,
    build_app,
    claim,
    console_client,
    console_headers,
    seed_jellyfin,
    set_up_server,
    web_login,
)
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.api.cookies import SECURE_NAMES

ME = f"{API}/me"
SESSIONS = f"{API}/me/sessions"


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signed_in_app(
    client: TestClient, username: str = USER_NAME, password: str = USER_PASSWORD
) -> str:
    """Sign a phone in and return its access token."""
    response = app_login(client, username, password)
    assert response.status_code == 200, response.text
    token: str = response.json()["access_token"]
    return token


# --- who is signed in -------------------------------------------------------------------


def test_the_console_reads_its_own_user(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.get(ME)
    assert response.status_code == 200
    assert_matches_contract(ME, "get", response)
    assert response.json()["name"] == "alex"
    assert response.json()["role"] == "admin"
    assert response.json()["media_server_admin"] is True


def test_the_app_reads_its_own_user(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        token = signed_in_app(client)
        response = client.get(ME, headers=bearer(token))
    assert response.status_code == 200
    assert_matches_contract(ME, "get", response)
    assert response.json()["name"] == "robin"
    assert response.json()["role"] == "user"


def test_a_setup_session_never_reaches_me(app: FastAPI) -> None:
    with console_client(app) as client:
        claim(client, app)
        assert_is_problem(client.get(ME), 401, "unauthorized")


# --- the caller's sessions ----------------------------------------------------------------


def test_listing_the_sessions_marks_the_current_one(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        token = signed_in_app(client, "alex", "correct horse")
        response = client.get(SESSIONS)
        assert response.status_code == 200
        assert_matches_contract(SESSIONS, "get", response)
        sessions = response.json()["sessions"]
        assert {row["kind"] for row in sessions} == {"web", "mobile"}
        web = next(row for row in sessions if row["kind"] == "web")
        phone = next(row for row in sessions if row["kind"] == "mobile")
        assert web["current"] is True
        assert phone["current"] is False
        assert phone["device_name"] == APP_DEVICE["name"]
        assert phone["platform"] == "android"
        assert phone["app_version"] == APP_DEVICE["app_version"]
        assert web["platform"] == "web"
        assert web["device_name"]

        # The same list, read by the phone, marks the phone instead.
        from_phone = client.get(SESSIONS, headers=bearer(token))
        assert_matches_contract(SESSIONS, "get", from_phone)
        mobile = next(row for row in from_phone.json()["sessions"] if row["kind"] == "mobile")
        assert mobile["current"] is True


def test_revoking_one_of_the_callers_sessions(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        token = signed_in_app(client, "alex", "correct horse")
        phone = next(
            row for row in client.get(SESSIONS).json()["sessions"] if row["kind"] == "mobile"
        )
        revoked = client.delete(
            f"{SESSIONS}/{phone['id']}", headers=console_headers(csrf_of(client))
        )
        assert revoked.status_code == 204
        assert_is_problem(client.get(ME, headers=bearer(token)), 401, "unauthorized")
        assert [row["kind"] for row in client.get(SESSIONS).json()["sessions"]] == ["web"]


def test_revoking_the_current_session_clears_its_cookie(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        mine = next(row for row in client.get(SESSIONS).json()["sessions"] if row["current"])
        revoked = client.delete(
            f"{SESSIONS}/{mine['id']}", headers=console_headers(csrf_of(client))
        )
        assert revoked.status_code == 204
        assert "Max-Age=0" in revoked.headers["set-cookie"]
        assert_is_problem(client.get(ME), 401, "unauthorized")


def test_another_users_session_is_not_found(app: FastAPI) -> None:
    with console_client(app) as admin:
        set_up_server(admin, app)
        mine = next(row for row in admin.get(SESSIONS).json()["sessions"] if row["current"])
    with console_client(app) as user:
        token = web_login(user, "robin", "another one").json()["csrf_token"]
        assert_is_problem(
            user.delete(f"{SESSIONS}/{mine['id']}", headers=console_headers(token)),
            404,
            "not_found",
        )
        assert_is_problem(
            user.delete(f"{SESSIONS}/nothing", headers=console_headers(token)), 404, "not_found"
        )
    # The administrator's session still works.
    with console_client(app) as admin:
        assert web_login(admin, "alex", "correct horse").status_code == 200


def test_a_phone_cannot_revoke_a_session(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        token = signed_in_app(client, "alex", "correct horse")
        phone = next(
            row for row in client.get(SESSIONS).json()["sessions"] if row["kind"] == "mobile"
        )
        response = client.delete(f"{SESSIONS}/{phone['id']}", headers=bearer(token))
        assert_is_problem(response, 401, "unauthorized")


# --- deleting the account's data --------------------------------------------------------------


def csrf_of(client: TestClient) -> str:
    """The CSRF token of the session the client holds."""
    token: str = client.get(f"{API}/auth/web/session").json()["csrf_token"]
    return token


def test_deleting_everything_signs_the_user_out_and_forgets_them(app: FastAPI) -> None:
    with console_client(app) as admin:
        set_up_server(admin, app)
    with console_client(app) as user:
        csrf = web_login(user, "robin", "another one").json()["csrf_token"]
        phone_token = signed_in_app(user)
        response = user.delete(
            ME, params={"confirm": "delete-my-data"}, headers=console_headers(csrf)
        )
        assert response.status_code == 204
        assert "Max-Age=0" in response.headers["set-cookie"]
        assert_is_problem(user.get(ME), 401, "unauthorized")
        # Their phone goes too: the session row went with the user row.
        assert_is_problem(user.get(ME, headers=bearer(phone_token)), 401, "unauthorized")
    with console_client(app) as admin:
        web_login(admin, "alex", "correct horse")
        assert [row["name"] for row in admin.get(f"{API}/admin/users").json()["users"]] == ["alex"]


def test_deleting_everything_needs_the_confirmation(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert_is_problem(client.delete(ME, headers=console_headers(csrf)), 400, "validation_error")
        assert_is_problem(
            client.delete(ME, params={"confirm": "yes"}, headers=console_headers(csrf)),
            400,
            "validation_error",
        )


def test_the_last_admin_cannot_delete_themselves(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.delete(
            ME, params={"confirm": "delete-my-data"}, headers=console_headers(csrf)
        )
        assert_is_problem(response, 409, "last_admin")
        assert client.get(ME).status_code == 200


def test_a_phone_cannot_delete_the_account(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        token = signed_in_app(client)
        response = client.delete(ME, params={"confirm": "delete-my-data"}, headers=bearer(token))
        assert_is_problem(response, 401, "unauthorized")


def test_deleting_everything_needs_the_csrf_token(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.delete(
            ME, params={"confirm": "delete-my-data"}, headers={"Origin": "https://console.test"}
        )
        assert_is_problem(response, 403, "csrf_failed")


def test_a_revoked_cookie_stops_working_at_once(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        cookie = client.cookies[SECURE_NAMES.session]
        mine = next(row for row in client.get(SESSIONS).json()["sessions"] if row["current"])
        client.delete(f"{SESSIONS}/{mine['id']}", headers=console_headers(csrf_of(client)))
    with console_client(app) as again:
        again.cookies.set(SECURE_NAMES.session, cookie)
        assert_is_problem(again.get(ME), 401, "unauthorized")


def test_an_unknown_query_parameter_is_ignored_but_the_confirmation_is_not(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response: Any = client.delete(
            ME, params={"confirm": "delete-my-data", "extra": "1"}, headers=console_headers(csrf)
        )
        # alex is the last admin, so the confirmation was read and the rule applied.
        assert_is_problem(response, 409, "last_admin")
