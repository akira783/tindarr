"""The console's administration endpoints: settings, connectors and users.

Check (a) of the roadmap for the admin scope of step 2 — the role rules of ADR 0010
(last admin, media server administrator, demotion clearing both halves), the locked
settings, and the contract validation of every new response.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    API,
    CONSOLE_ORIGIN,
    FakeClock,
    FakeInternet,
    build_app,
    console_client,
    console_headers,
    seed_jellyfin,
    set_up_server,
    web_login,
)
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.api.cookies import SECURE_NAMES

SETTINGS = f"{API}/admin/settings"
CONNECTORS = f"{API}/admin/connectors"
USERS = f"{API}/admin/users"


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet)


def csrf_of(client: TestClient) -> str:
    """The CSRF token of the session the client holds."""
    token: str = client.get(f"{API}/auth/web/session").json()["csrf_token"]
    return token


def user_named(client: TestClient, name: str) -> Any:
    return next(row for row in client.get(USERS).json()["users"] if row["name"] == name)


def set_up_with_two_users(app: FastAPI) -> None:
    """Set the server up (alex, an administrator) and sign robin in as well."""
    with console_client(app) as client:
        set_up_server(client, app)
    with console_client(app) as user:
        assert web_login(user, "robin", "another one").status_code == 200


# --- settings ---------------------------------------------------------------------------


def test_reading_the_settings(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.get(SETTINGS)
    assert response.status_code == 200
    assert_matches_contract(SETTINGS, "get", response)
    body = response.json()
    assert body["name"] == "Tindarr"
    assert body["public_url"] is None
    assert body["password_sign_in"] == "enabled"
    assert body["daily_generation_limit"] == 10
    assert body["content_filters"]["exclude_adult"] is True
    assert body["locked_fields"] == []


def test_every_field_is_stored_even_the_ones_step_4_will_use(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        patch = {
            "name": "Chez nous",
            "language": "fr-FR",
            "streaming_region": "FR",
            "daily_generation_limit": 25,
            "warm_up_enabled": False,
            "content_filters": {
                "exclude_adult": False,
                "min_year": 1980,
                "excluded_genres": ["Horror"],
                "excluded_original_languages": ["ru"],
            },
        }
        response = client.patch(SETTINGS, json=patch, headers=console_headers(csrf))
        assert response.status_code == 200, response.text
        assert_matches_contract(SETTINGS, "patch", response)
        for field, value in patch.items():
            assert response.json()[field] == value
        # Stored, so a later read says the same thing.
        assert client.get(SETTINGS).json() == response.json()
        # ``name`` acts now.
        assert client.get(f"{API}/server/info").json()["name"] == "Chez nous"


def test_password_sign_in_takes_effect_at_once(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        changed = client.patch(
            SETTINGS, json={"password_sign_in": "disabled"}, headers=console_headers(csrf)
        )
        assert changed.status_code == 200
        assert "password" not in client.get(f"{API}/server/info").json()["auth_methods"]
    with console_client(app) as other:
        assert_is_problem(web_login(other), 403, "password_sign_in_disabled")


def test_password_sign_in_is_reserved_to_a_media_server_admin(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as admin:
        signed_in = web_login(admin, "alex", "correct horse")
        robin = user_named(admin, "robin")
        assert (
            admin.patch(
                f"{USERS}/{robin['id']}",
                json={"role": "admin"},
                headers=console_headers(signed_in.json()["csrf_token"]),
            ).status_code
            == 200
        )
    with console_client(app) as promoted:
        token = web_login(promoted, "robin", "another one").json()["csrf_token"]
        response = promoted.patch(
            SETTINGS, json={"password_sign_in": "lan_only"}, headers=console_headers(token)
        )
        assert_is_problem(response, 403, "media_server_admin_required")


def test_a_locked_setting_refuses_the_whole_patch(
    data_dir: Path, clock: FakeClock, internet: FakeInternet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_SERVER_NAME", "Fixed by the operator")
    app = build_app(data_dir, clock=clock, internet=internet)
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(
            SETTINGS,
            json={"name": "Something else", "language": "fr"},
            headers=console_headers(csrf),
        )
        assert_is_problem(response, 409, "setting_locked")
        body = client.get(SETTINGS).json()
        assert body["name"] == "Fixed by the operator"
        assert body["language"] == "en"
        assert body["locked_fields"] == ["name"]


def test_an_empty_patch_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert_is_problem(
            client.patch(SETTINGS, json={}, headers=console_headers(csrf)), 400, "validation_error"
        )


@pytest.mark.parametrize("field", ["name", "password_sign_in", "language", "warm_up_enabled"])
def test_a_setting_that_cannot_be_cleared_refuses_null(app: FastAPI, field: str) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(SETTINGS, json={field: None}, headers=console_headers(csrf))
        assert_is_problem(response, 400, "validation_error")


def test_an_unknown_field_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(SETTINGS, json={"not_a_setting": 1}, headers=console_headers(csrf))
        assert_is_problem(response, 400, "validation_error")


def test_the_settings_are_admins_only(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as user:
        token = web_login(user, "robin", "another one").json()["csrf_token"]
        assert_is_problem(user.get(SETTINGS), 403, "admin_required")
        assert_is_problem(
            user.patch(SETTINGS, json={"name": "x"}, headers=console_headers(token)),
            403,
            "admin_required",
        )


def test_a_bearer_token_never_reaches_the_settings(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.get(SETTINGS, headers={"Authorization": "Bearer something"})
        assert_is_problem(response, 401, "unauthorized")


def test_changing_the_settings_needs_the_csrf_token(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.patch(SETTINGS, json={"name": "x"}, headers={"Origin": CONSOLE_ORIGIN})
        assert_is_problem(response, 403, "csrf_failed")


# --- connectors --------------------------------------------------------------------------


def test_listing_the_connectors_shows_every_kind(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.get(CONNECTORS)
    assert response.status_code == 200
    assert_matches_contract(CONNECTORS, "get", response)
    connectors = {item["kind"]: item for item in response.json()["connectors"]}
    assert set(connectors) == {"media_server", "requests", "tmdb", "omdb", "llm"}
    assert connectors["media_server"]["configured"] is True
    assert connectors["media_server"]["provider"] == "jellyfin"
    # Listing never calls anything: the health is "not tested here".
    assert connectors["media_server"]["status"]["health"] == "unknown"
    assert connectors["media_server"]["status"]["checked_at"] is None
    assert connectors["media_server"]["secret"]["set"] is True
    assert connectors["media_server"]["secret"]["last4"] is not None
    for kind in ("requests", "tmdb", "omdb", "llm"):
        assert connectors[kind]["configured"] is False
        assert connectors[kind]["status"]["health"] == "not_configured"
        assert connectors[kind]["secret"] == {"set": False, "last4": None, "locked": False}


def test_the_stored_api_key_is_never_returned(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        body = client.get(CONNECTORS).text
    assert "admin-api-key" not in body
    assert "api-key" not in body


def test_listing_the_connectors_is_admins_only(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as user:
        web_login(user, "robin", "another one")
        assert_is_problem(user.get(CONNECTORS), 403, "admin_required")


def test_the_media_server_connector_cannot_be_removed(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.delete(f"{CONNECTORS}/media_server", headers=console_headers(csrf))
        assert_is_problem(response, 409, "media_server_required")
        assert client.get(CONNECTORS).json()["connectors"][0]["configured"] is True


@pytest.mark.parametrize("kind", ["requests", "tmdb", "omdb", "llm"])
def test_removing_an_optional_connector_nobody_configured_is_not_an_error(
    app: FastAPI, kind: str
) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.delete(f"{CONNECTORS}/{kind}", headers=console_headers(csrf))
        assert response.status_code == 204, response.text
        listed = {row["kind"]: row for row in client.get(CONNECTORS).json()["connectors"]}
        assert listed[kind]["configured"] is False
        assert listed[kind]["status"]["health"] == "not_configured"


# --- users ------------------------------------------------------------------------------


def test_listing_the_users(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as client:
        web_login(client, "alex", "correct horse")
        response = client.get(USERS)
    assert response.status_code == 200
    assert_matches_contract(USERS, "get", response)
    users = {row["name"]: row for row in response.json()["users"]}
    assert users["alex"]["role"] == "admin"
    assert users["alex"]["media_server_admin"] is True
    assert users["robin"]["role"] == "user"
    assert users["robin"]["enabled"] is True
    assert users["robin"]["votes"] == 0
    assert users["robin"]["generations_today"] == 0
    assert users["robin"]["daily_generation_limit"] is None


def test_promoting_and_demoting_a_user(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as client:
        csrf = web_login(client, "alex", "correct horse").json()["csrf_token"]
        robin = user_named(client, "robin")
        promoted = client.patch(
            f"{USERS}/{robin['id']}", json={"role": "admin"}, headers=console_headers(csrf)
        )
        assert promoted.status_code == 200
        assert_matches_contract(f"{USERS}/{{user_id}}", "patch", promoted)
        assert promoted.json()["promoted"] is True
        assert promoted.json()["role"] == "admin"

        demoted = client.patch(
            f"{USERS}/{robin['id']}", json={"role": "user"}, headers=console_headers(csrf)
        )
        assert demoted.json()["promoted"] is False
        assert demoted.json()["media_server_admin"] is False
        assert demoted.json()["role"] == "user"


def test_disabling_a_user_revokes_their_sessions(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as user:
        web_login(user, "robin", "another one")
        robins_cookie = user.cookies[SECURE_NAMES.session]
    with console_client(app) as admin:
        csrf = web_login(admin, "alex", "correct horse").json()["csrf_token"]
        robin = user_named(admin, "robin")
        disabled = admin.patch(
            f"{USERS}/{robin['id']}", json={"enabled": False}, headers=console_headers(csrf)
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["disabled_reason"] == "admin"
    # The session robin was holding stops working on its very next request.
    with console_client(app) as robins_browser:
        robins_browser.cookies.set(SECURE_NAMES.session, robins_cookie)
        assert_is_problem(robins_browser.get(f"{API}/me"), 401, "unauthorized")


def test_re_enabling_a_user_clears_the_reason(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as admin:
        csrf = web_login(admin, "alex", "correct horse").json()["csrf_token"]
        robin = user_named(admin, "robin")
        admin.patch(
            f"{USERS}/{robin['id']}", json={"enabled": False}, headers=console_headers(csrf)
        )
        enabled = admin.patch(
            f"{USERS}/{robin['id']}", json={"enabled": True}, headers=console_headers(csrf)
        )
        assert enabled.json()["enabled"] is True
        assert enabled.json()["disabled_reason"] is None


def test_setting_a_generation_limit(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as admin:
        csrf = web_login(admin, "alex", "correct horse").json()["csrf_token"]
        robin = user_named(admin, "robin")
        limited = admin.patch(
            f"{USERS}/{robin['id']}",
            json={"daily_generation_limit": 3},
            headers=console_headers(csrf),
        )
        assert limited.json()["daily_generation_limit"] == 3
        cleared = admin.patch(
            f"{USERS}/{robin['id']}",
            json={"daily_generation_limit": None},
            headers=console_headers(csrf),
        )
        assert cleared.json()["daily_generation_limit"] is None


def test_the_last_admin_cannot_be_demoted_or_disabled(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        alex = user_named(client, "alex")
        for patch in ({"role": "user"}, {"enabled": False}):
            refused = client.patch(
                f"{USERS}/{alex['id']}", json=patch, headers=console_headers(csrf)
            )
            assert_is_problem(refused, 409, "last_admin")
        assert client.get(f"{API}/me").json()["role"] == "admin"


def test_a_promoted_admin_cannot_touch_a_media_server_admin(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as admin:
        csrf = web_login(admin, "alex", "correct horse").json()["csrf_token"]
        robin = user_named(admin, "robin")
        admin.patch(f"{USERS}/{robin['id']}", json={"role": "admin"}, headers=console_headers(csrf))
    with console_client(app) as promoted:
        token = web_login(promoted, "robin", "another one").json()["csrf_token"]
        alex = user_named(promoted, "alex")
        for patch in ({"role": "user"}, {"enabled": False}):
            refused = promoted.patch(
                f"{USERS}/{alex['id']}", json=patch, headers=console_headers(token)
            )
            assert_is_problem(refused, 403, "forbidden")
        # Signing them out repeatedly would achieve the same thing, so it is refused too.
        assert_is_problem(
            promoted.delete(f"{USERS}/{alex['id']}/sessions", headers=console_headers(token)),
            403,
            "forbidden",
        )
        # But they may still change something that takes nothing away.
        allowed = promoted.patch(
            f"{USERS}/{alex['id']}",
            json={"daily_generation_limit": 5},
            headers=console_headers(token),
        )
        assert allowed.status_code == 200


def test_an_unknown_user_is_not_found(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert_is_problem(
            client.patch(f"{USERS}/nobody", json={"enabled": True}, headers=console_headers(csrf)),
            404,
            "not_found",
        )
        assert_is_problem(
            client.delete(f"{USERS}/nobody/sessions", headers=console_headers(csrf)),
            404,
            "not_found",
        )


def test_an_empty_user_patch_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        alex = user_named(client, "alex")
        assert_is_problem(
            client.patch(f"{USERS}/{alex['id']}", json={}, headers=console_headers(csrf)),
            400,
            "validation_error",
        )
        assert_is_problem(
            client.patch(
                f"{USERS}/{alex['id']}", json={"enabled": None}, headers=console_headers(csrf)
            ),
            400,
            "validation_error",
        )


def test_signing_a_user_out_of_every_device(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as user:
        web_login(user, "robin", "another one")
        robins_cookie = user.cookies[SECURE_NAMES.session]
    with console_client(app) as admin:
        csrf = web_login(admin, "alex", "correct horse").json()["csrf_token"]
        robin = user_named(admin, "robin")
        revoked = admin.delete(f"{USERS}/{robin['id']}/sessions", headers=console_headers(csrf))
        assert revoked.status_code == 204
    with console_client(app) as robins_browser:
        robins_browser.cookies.set(SECURE_NAMES.session, robins_cookie)
        assert_is_problem(robins_browser.get(f"{API}/me"), 401, "unauthorized")


def test_the_user_endpoints_are_admins_only(app: FastAPI) -> None:
    set_up_with_two_users(app)
    with console_client(app) as user:
        token = web_login(user, "robin", "another one").json()["csrf_token"]
        robin = user.get(f"{API}/me").json()
        assert_is_problem(user.get(USERS), 403, "admin_required")
        assert_is_problem(
            user.patch(
                f"{USERS}/{robin['id']}", json={"enabled": True}, headers=console_headers(token)
            ),
            403,
            "admin_required",
        )
        assert_is_problem(
            user.delete(f"{USERS}/{robin['id']}/sessions", headers=console_headers(token)),
            403,
            "admin_required",
        )
