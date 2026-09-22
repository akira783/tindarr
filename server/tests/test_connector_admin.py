"""Changing the media server after setup, and the hourly user sync."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    ADMIN_API_KEY,
    ADMIN_NAME,
    API,
    MEDIA_SERVER_URL,
    USER_NAME,
    USER_PASSWORD,
    FakeClock,
    FakeInternet,
    app_login,
    build_app,
    claim,
    complete_setup,
    console_client,
    console_headers,
    run,
    seed_jellyfin,
    set_up_server,
    web_login,
)
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.api.cookies import SECURE_NAMES
from tindarr.auth.sync import UserSync
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction

CONNECTOR = f"{API}/admin/connectors/media_server"
OTHER_SERVER_ID = "1" * 32


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet)


def connector_body(**fields: Any) -> dict[str, Any]:
    return {
        "connector": "media_server",
        "server_type": "jellyfin",
        "url": MEDIA_SERVER_URL,
        "api_key": ADMIN_API_KEY,
    } | fields


def users_of(client: TestClient, app: FastAPI) -> Any:
    """Every user row, read from inside the running application."""
    services: Any = app.state.services

    async def read() -> Any:
        async with services.engine.connect() as connection:
            return await user_repository.list_all(connection)

    return run(client, read)


def set_user_fields(client: TestClient, app: FastAPI, user_id: str, **fields: Any) -> None:
    """Change a user row from inside the running application."""
    services: Any = app.state.services

    async def write() -> None:
        async with write_transaction(services.engine) as connection:
            await user_repository.update_fields(connection, user_id, **fields)

    run(client, write)


# --- who may repoint the media server ---------------------------------------------------


def test_a_media_server_administrator_can_save_the_connector(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
    assert_matches_contract(f"{API}/admin/connectors/{{kind}}", "put", response)
    body = response.json()
    assert body["kind"] == "media_server"
    assert body["configured"] is True
    assert body["provider"] == "jellyfin"
    assert body["status"]["health"] == "ok"
    assert body["secret"]["set"] is True
    assert ADMIN_API_KEY not in str(body)


def test_a_promoted_administrator_cannot_repoint_the_media_server(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        client.cookies.clear()
        csrf = web_login(client, USER_NAME, USER_PASSWORD).json()["csrf_token"]
        user = next(u for u in users_of(client, app) if u.name == USER_NAME)
        set_user_fields(client, app, user.id, promoted=True)
        response = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
    assert_is_problem(response, 403, "media_server_admin_required")


def test_someone_who_is_not_an_administrator_at_all_is_refused(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        client.cookies.clear()
        csrf = web_login(client, USER_NAME, USER_PASSWORD).json()["csrf_token"]
        response = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
    assert_is_problem(response, 403, "admin_required")


def test_the_connector_needs_a_fresh_re_authentication(app: FastAPI, clock: FakeClock) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        clock.advance(6 * 60)
        response = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
        assert_is_problem(response, 403, "reauth_required")
        assert (
            client.post(
                f"{API}/auth/web/reauth",
                json={"password": "correct horse"},
                headers=console_headers(csrf),
            ).status_code
            == 200
        )
        again = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
    assert again.status_code == 200, again.text


def test_the_connector_needs_the_csrf_token(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = client.put(CONNECTOR, json=connector_body(), headers=console_headers())
    assert_is_problem(response, 403, "csrf_failed")


def test_the_other_connector_kinds_do_not_exist_yet(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.put(
            f"{API}/admin/connectors/tmdb", json=connector_body(), headers=console_headers(csrf)
        )
    assert_is_problem(response, 404, "not_found")


# --- what an identity change costs -------------------------------------------------------


def test_pointing_at_another_server_signs_everyone_out_and_unlinks_them(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        internet.media.server_id = OTHER_SERVER_ID
        response = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
        assert response.status_code == 200, response.text
        # The caller's own session went with the rest, and its cookie was cleared.
        assert "Max-Age=0" in response.headers["set-cookie"]
        users = users_of(client, app)
        after = client.get(f"{API}/auth/web/session")
    assert [user.media_server_user_id for user in users] == [None, None]
    assert all(user.disabled_reason == "unlinked" for user in users)
    assert after.status_code == 401


def test_the_same_server_at_a_new_address_keeps_everyone_signed_in(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.put(
            CONNECTOR,
            json=connector_body(url="http://media.lan:8096/"),
            headers=console_headers(csrf),
        )
        assert response.status_code == 200
        assert "set-cookie" not in response.headers
        assert client.get(f"{API}/auth/web/session").status_code == 200


def test_an_unexpected_identity_stops_sign_ins(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.server_id = OTHER_SERVER_ID
        # The identity is re-read at most every five minutes.
        clock.advance(5 * 60 + 1)
        response = app_login(client, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 503, "media_server_changed")


def test_the_identity_is_not_re_read_on_every_sign_in(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        before = sum(1 for r in internet.media.requests if r.url.path == "/System/Info/Public")
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        after = sum(1 for r in internet.media.requests if r.url.path == "/System/Info/Public")
    assert after == before


# --- the test endpoint -------------------------------------------------------------------


def stored_connector(client: TestClient, app: FastAPI) -> Any:
    """The connector as it is stored, read from inside the running application."""
    services: Any = app.state.services
    return run(client, services.connector.configured)


def test_a_connector_can_be_tested_without_saving(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        before = stored_connector(client, app)
        elsewhere = client.post(
            f"{CONNECTOR}/test",
            json=connector_body(url="http://elsewhere.lan:8096", api_key="another"),
            headers=console_headers(csrf),
        )
        assert_matches_contract(f"{API}/admin/connectors/{{kind}}/test", "post", elsewhere)
        # Nothing answers at that address, which is what a test is for.
        assert elsewhere.json()["health"] == "unreachable"
        wrong_key = client.post(
            f"{CONNECTOR}/test",
            json=connector_body(api_key="another"),
            headers=console_headers(csrf),
        )
        assert wrong_key.json()["health"] == "unauthorized"
        # Neither test wrote anything: the stored connector is untouched.
        assert stored_connector(client, app) == before


def test_an_old_jellyfin_is_reported_as_unsupported(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        internet.media.version = "10.9.11"
        response = client.post(
            f"{CONNECTOR}/test", json=connector_body(), headers=console_headers(csrf)
        )
        assert response.json()["health"] == "unsupported_version"
        saved = client.put(CONNECTOR, json=connector_body(), headers=console_headers(csrf))
    assert_is_problem(saved, 502, "media_server_unsupported")


def test_tests_are_limited_per_session(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        for _ in range(10):
            assert (
                client.post(
                    f"{CONNECTOR}/test", json=connector_body(), headers=console_headers(csrf)
                ).status_code
                == 200
            )
        response = client.post(
            f"{CONNECTOR}/test", json=connector_body(), headers=console_headers(csrf)
        )
    assert_is_problem(response, 429, "rate_limited")


# --- the hourly sync -----------------------------------------------------------------------


def sync_of(app: FastAPI) -> UserSync:
    services: Any = app.state.services
    return UserSync(services.engine, services.connector, services.clock, services.install_id)


def test_the_sync_disables_a_user_the_media_server_no_longer_has(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        tokens = app_login(client, USER_NAME, USER_PASSWORD).json()
        del internet.media.users[USER_NAME]
        result = run(client, sync_of(app).run)
        assert result.disabled == 1
        assert result.revoked == 1
        user = next(u for u in users_of(client, app) if u.name == USER_NAME)
        assert (user.enabled, user.disabled_reason) == (False, "media_server")
        response = client.post(
            f"{API}/auth/logout", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
    assert_is_problem(response, 401, "unauthorized")


def test_the_sync_disables_a_user_disabled_on_the_media_server(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        internet.media.users[USER_NAME]["Policy"]["IsDisabled"] = True
        assert run(client, sync_of(app).run).disabled == 1
        user = next(u for u in users_of(client, app) if u.name == USER_NAME)
    assert user.enabled is False


def test_the_sync_clears_an_administrator_flag_but_never_sets_one(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        internet.media.users[ADMIN_NAME]["Policy"]["IsAdministrator"] = False
        internet.media.users[USER_NAME]["Policy"]["IsAdministrator"] = True
        assert run(client, sync_of(app).run).demoted == 1
        by_name = {user.name: user for user in users_of(client, app)}
    assert by_name[ADMIN_NAME].media_server_admin is False
    # Granting is a sign-in's job only (docs/adr/0010).
    assert by_name[USER_NAME].media_server_admin is False


def test_the_sync_never_overwrites_a_ban_an_administrator_decided(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        user = next(u for u in users_of(client, app) if u.name == USER_NAME)
        set_user_fields(client, app, user.id, enabled=False, disabled_reason="admin")
        del internet.media.users[USER_NAME]
        assert run(client, sync_of(app).run).disabled == 0
        assert next(u for u in users_of(client, app) if u.name == USER_NAME).disabled_reason == (
            "admin"
        )
        # Re-added on the media server, the ban still stands: only a user disabled
        # *by* the media server is re-enabled when it accepts them again.
        internet.media.add_user(USER_NAME, USER_PASSWORD, user_id="b" * 32, admin=False)
        response = app_login(client, USER_NAME, USER_PASSWORD)
    assert_is_problem(response, 403, "account_disabled")


def test_the_sync_reports_a_user_it_disabled_only_once(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        assert app_login(client, USER_NAME, USER_PASSWORD).status_code == 200
        del internet.media.users[USER_NAME]
        assert run(client, sync_of(app).run).disabled == 1
        # An hour later the same absence is not news any more.
        assert run(client, sync_of(app).run).disabled == 0


def test_a_media_server_that_lists_nobody_changes_nothing(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.users.clear()
        result = run(client, sync_of(app).run)
        users = users_of(client, app)
    assert result.ran is False
    assert all(user.enabled for user in users)


def test_the_sync_follows_the_name_and_the_remote_access_policy(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.users[ADMIN_NAME]["Name"] = "Alexandra"
        internet.media.users[ADMIN_NAME]["Policy"]["EnableRemoteAccess"] = False
        run(client, sync_of(app).run)
        user = users_of(client, app)[0]
    assert (user.name, user.remote_access) == ("Alexandra", False)


def test_an_unreachable_media_server_changes_nothing(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.offline = True
        result = run(client, sync_of(app).run)
        user = users_of(client, app)[0]
    assert result.ran is False
    assert user.enabled is True


def test_an_unexpected_identity_changes_nothing(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.server_id = OTHER_SERVER_ID
        clock.advance(5 * 60 + 1)
        result = run(client, sync_of(app).run)
        user = users_of(client, app)[0]
    assert result.ran is False
    assert user.enabled is True


def test_the_sync_does_nothing_without_a_media_server(app: FastAPI) -> None:
    with console_client(app) as client:
        assert run(client, sync_of(app).run).ran is False
        assert client.cookies.get(SECURE_NAMES.session) is None


def test_turning_tls_verification_off_needs_the_key_again(app: FastAPI) -> None:
    # Omitting the key replays the stored administrator key; sending it over a
    # connection nobody checks is all an on-path attacker needs.
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        body = connector_body(verify_tls=False)
        del body["api_key"]
        response = client.post(f"{CONNECTOR}/test", json=body, headers=console_headers(csrf))
        assert_is_problem(response, 409, "secret_required")
        # With the key, the downgrade is the administrator's call to make.
        with_key = client.post(
            f"{CONNECTOR}/test",
            json=connector_body(verify_tls=False),
            headers=console_headers(csrf),
        )
        assert with_key.status_code == 200


def test_a_short_api_key_is_never_shown_in_full(app: FastAPI, internet: FakeInternet) -> None:
    internet.media.api_key = "abcd"
    with console_client(app) as client:
        csrf = claim(client, app)
        saved = client.put(
            f"{API}/setup/media-server",
            json=connector_body(api_key="abcd"),
            headers=console_headers(csrf),
        )
        assert saved.status_code == 200, saved.text
        complete_setup(client, app)
        connectors = client.get(f"{API}/admin/connectors").json()["connectors"]
    media_server = next(item for item in connectors if item["kind"] == "media_server")
    assert media_server["secret"] == {"set": True, "last4": None, "locked": False}
