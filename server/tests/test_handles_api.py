"""Quick Connect, Plex PINs, the handles behind them, and step-up re-authentication."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    ADMIN_NAME,
    API,
    APP_DEVICE,
    MACHINE_ID,
    PLEX_SERVER_URL,
    USER_NAME,
    USER_PASSWORD,
    FakeClock,
    FakeInternet,
    build_app,
    claim,
    console_client,
    console_headers,
    seed_jellyfin,
    set_up_server,
)
from tests.support.flows import configure_media_server
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindeerr.api.cookies import SECURE_NAMES
from tindeerr.auth.handles import MAX_TOTAL, Binding, pkce_challenge

VERIFIER = "a" * 43
CHALLENGE = pkce_challenge(VERIFIER)
OWNER_TOKEN = "plex-owner-token"
OWNER_ID = "42"
FRIEND_TOKEN = "plex-friend-token"
FRIEND_ID = "77"


@pytest.fixture
def internet() -> FakeInternet:
    fake = seed_jellyfin(FakeInternet())
    fake.plex_tv.add_account(OWNER_TOKEN, OWNER_ID, "Alex")
    return fake


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet)


def last_plex_code(internet: FakeInternet) -> str:
    """The code of the PIN plex.tv created most recently."""
    return list(internet.plex_tv.pins.values())[-1].code


def start_quick_connect(client: TestClient, csrf: str | None = None, **body: Any) -> Any:
    return client.post(
        f"{API}/auth/quick-connect",
        json={"purpose": "sign_in"} | body,
        headers=console_headers(csrf),
    )


def start_plex_pin(client: TestClient, csrf: str | None = None, **body: Any) -> Any:
    return client.post(
        f"{API}/auth/plex/pins",
        json={"purpose": "sign_in"} | body,
        headers=console_headers(csrf),
    )


# --- Quick Connect, from the console ---------------------------------------------------


def test_the_console_signs_in_with_quick_connect(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        started = start_quick_connect(client)
        assert_matches_contract(f"{API}/auth/quick-connect", "post", started)
        assert started.status_code == 201
        handle = started.json()["handle"]
        assert client.cookies.get(SECURE_NAMES.preauth) is not None
        body = {"handle": handle}
        pending = client.post(
            f"{API}/auth/web/quick-connect/login", json=body, headers=console_headers()
        )
        assert_matches_contract(f"{API}/auth/web/quick-connect/login", "post", pending)
        assert pending.status_code == 202
        assert pending.json()["pending"] is True
        assert pending.headers["Retry-After"]
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", ADMIN_NAME)
        clock.advance(2)
        signed_in = client.post(
            f"{API}/auth/web/quick-connect/login", json=body, headers=console_headers()
        )
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["setup_completed_now"] is True
    # Jellyfin opened the session at the approval; collecting it closed it again.
    assert internet.media.logouts == [f"user-token-{ADMIN_NAME}"]


def test_a_quick_connect_handle_belongs_to_the_browser_that_started_it(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        handle = start_quick_connect(client).json()["handle"]
    with console_client(app) as other:
        response = other.post(
            f"{API}/auth/web/quick-connect/login",
            json={"handle": handle},
            headers=console_headers(),
        )
    assert_is_problem(response, 410, "quick_connect_expired")


def test_quick_connect_is_refused_when_the_server_switched_it_off(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        internet.media.quick_connect_enabled = False
        response = start_quick_connect(client)
    assert_is_problem(response, 409, "quick_connect_unavailable")


def test_quick_connect_is_refused_on_emby(app: FastAPI, internet: FakeInternet) -> None:
    internet.media.kind = "emby"
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf, server_type="emby").status_code == 200
        response = start_quick_connect(client)
    assert_is_problem(response, 409, "quick_connect_unavailable")


# --- Quick Connect, from the app ----------------------------------------------------------


def test_the_app_signs_in_with_quick_connect(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        started = client.post(
            f"{API}/auth/quick-connect", json={"purpose": "sign_in", "code_challenge": CHALLENGE}
        )
        assert started.status_code == 201
        handle = started.json()["handle"]
        body = {"handle": handle, "code_verifier": VERIFIER, "device": APP_DEVICE}
        assert client.post(f"{API}/auth/quick-connect/login", json=body).status_code == 202
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", USER_NAME)
        clock.advance(2)
        response = client.post(f"{API}/auth/quick-connect/login", json=body)
    assert_matches_contract(f"{API}/auth/quick-connect/login", "post", response)
    assert response.json()["user"]["name"] == USER_NAME


def test_a_wrong_code_verifier_is_an_unknown_handle(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        handle = client.post(
            f"{API}/auth/quick-connect", json={"purpose": "sign_in", "code_challenge": CHALLENGE}
        ).json()["handle"]
        response = client.post(
            f"{API}/auth/quick-connect/login",
            json={"handle": handle, "code_verifier": "b" * 43, "device": APP_DEVICE},
        )
    assert_is_problem(response, 410, "quick_connect_expired")


def test_a_handle_is_used_once(app: FastAPI, internet: FakeInternet, clock: FakeClock) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        handle = client.post(
            f"{API}/auth/quick-connect", json={"purpose": "sign_in", "code_challenge": CHALLENGE}
        ).json()["handle"]
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", USER_NAME)
        body = {"handle": handle, "code_verifier": VERIFIER, "device": APP_DEVICE}
        assert client.post(f"{API}/auth/quick-connect/login", json=body).status_code == 200
        clock.advance(2)
        response = client.post(f"{API}/auth/quick-connect/login", json=body)
    assert_is_problem(response, 410, "quick_connect_expired")


def test_a_handle_expires(app: FastAPI, clock: FakeClock) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        handle = client.post(
            f"{API}/auth/quick-connect", json={"purpose": "sign_in", "code_challenge": CHALLENGE}
        ).json()["handle"]
        clock.advance(5 * 60 + 1)
        response = client.post(
            f"{API}/auth/quick-connect/login",
            json={"handle": handle, "code_verifier": VERIFIER, "device": APP_DEVICE},
        )
    assert_is_problem(response, 410, "quick_connect_expired")


def test_a_sign_in_handle_cannot_be_used_as_a_step_up(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        handle = start_quick_connect(client).json()["handle"]
        response = client.post(
            f"{API}/auth/web/reauth", json={"handle": handle}, headers=console_headers(csrf)
        )
    assert_is_problem(response, 410, "quick_connect_expired")


# --- the caps on outstanding handles -----------------------------------------------------------


def test_only_five_handles_may_be_outstanding_per_address(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        for _ in range(5):
            assert start_quick_connect(client).status_code == 201
        response = start_quick_connect(client)
    assert_is_problem(response, 429, "rate_limited")


def test_handle_creation_is_limited_over_time(app: FastAPI, clock: FakeClock) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        for _ in range(2):
            for _ in range(5):
                assert start_quick_connect(client).status_code == 201
            # Let the five outstanding ones expire; the creations still count.
            clock.advance(5 * 60 + 1)
        response = start_quick_connect(client)
    assert_is_problem(response, 429, "rate_limited")


def test_the_global_cap_refuses_rather_than_growing(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        services: Any = app.state.services
        # Two hundred outstanding handles is a memory bound, and the one global limit
        # that refuses instead of slowing down (docs/auth.md, section 8).
        for index in range(MAX_TOTAL - services.handles.outstanding):
            services.handles.create(
                "quick_connect",
                "sign_in",
                Binding.session(f"session-{index}"),
                client_key=f"10.0.{index // 250}.{index % 250}",
            )
        response = start_quick_connect(client)
    assert_is_problem(response, 429, "rate_limited")


# --- Plex --------------------------------------------------------------------------------------


def set_up_plex(client: TestClient, app: FastAPI, internet: FakeInternet, clock: FakeClock) -> str:
    """Claim, obtain a Plex owner token by PIN, and save the Plex connector."""
    csrf = claim(client, app)
    created = client.post(
        f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers(csrf)
    )
    assert created.status_code == 201, created.text
    pin_id = created.json()["pin_id"]
    assert (
        client.post(
            f"{API}/auth/plex/pins/status", json={"pin_id": pin_id}, headers=console_headers(csrf)
        ).json()["status"]
        == "pending"
    )
    internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
    clock.advance(2)
    status = client.post(
        f"{API}/auth/plex/pins/status", json={"pin_id": pin_id}, headers=console_headers(csrf)
    )
    assert_matches_contract(f"{API}/auth/plex/pins/status", "post", status)
    assert status.json() == {
        "status": "authorized",
        "expires_at": status.json()["expires_at"],
        "account_name": "Alex",
    }
    saved = client.put(
        f"{API}/setup/media-server",
        json={
            "connector": "media_server",
            "server_type": "plex",
            "url": PLEX_SERVER_URL,
            "plex_pin_id": pin_id,
        },
        headers=console_headers(csrf),
    )
    assert saved.status_code == 200, saved.text
    return csrf


def test_the_wizard_configures_plex_with_an_owner_token_pin(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        assert client.get(f"{API}/setup/state").json()["auth_methods"] == ["plex_pin"]


def test_a_plex_sign_in_needs_a_resource_with_the_stored_machine_identifier(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        pin_id = start_plex_pin(client).json()["pin_id"]
        internet.plex_tv.add_account(FRIEND_TOKEN, FRIEND_ID, "Robin", machine_id="another")
        internet.plex_tv.approve(last_plex_code(internet), FRIEND_TOKEN)
        clock.advance(2)
        response = client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
    assert_is_problem(response, 403, "not_a_server_user")


def test_the_owner_of_the_plex_resource_is_the_administrator(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        pin_id = start_plex_pin(client).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        clock.advance(2)
        response = client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
    assert_matches_contract(f"{API}/auth/web/plex/login", "post", response)
    body = response.json()
    assert body["setup_completed_now"] is True
    assert body["user"]["media_server_admin"] is True
    assert body["user"]["id"]


def test_a_shared_plex_account_is_not_an_administrator(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        pin_id = start_plex_pin(client).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        clock.advance(2)
        client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
        internet.plex_tv.add_account(FRIEND_TOKEN, FRIEND_ID, "Robin", owned=False)
        response = client.post(
            f"{API}/auth/plex/pins",
            json={"purpose": "sign_in", "code_challenge": CHALLENGE},
        )
        pin_id = response.json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), FRIEND_TOKEN)
        clock.advance(2)
        signed_in = client.post(
            f"{API}/auth/plex/login",
            json={"pin_id": pin_id, "code_verifier": VERIFIER, "device": APP_DEVICE},
        )
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["user"]["role"] == "user"
    assert signed_in.json()["user"]["id"] != ""


def test_the_plex_device_of_a_sign_in_is_deleted_afterwards(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        before = dict(internet.plex_tv.devices)
        pin_id = start_plex_pin(client).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        clock.advance(2)
        assert (
            client.post(
                f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
            ).status_code
            == 200
        )
    # Only the device of the owner-token PIN, which uses the install id, is left.
    assert internet.plex_tv.devices == before


def test_a_failing_device_deletion_does_not_fail_the_sign_in(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        pin_id = start_plex_pin(client).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        internet.plex_tv.device_deletion_fails = True
        clock.advance(2)
        response = client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
    assert response.status_code == 200, response.text


def test_plex_tv_asking_us_to_slow_down_is_passed_on(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        internet.plex_tv.rate_limited.add("/api/v2/pins")
        response = start_plex_pin(client)
    assert_is_problem(response, 429, "rate_limited")


def test_plex_tv_being_down_stops_a_pin(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        internet.plex_tv.offline = True
        response = start_plex_pin(client)
    assert_is_problem(response, 503, "plex_tv_unreachable")


def test_a_plex_pin_is_not_offered_when_the_server_is_jellyfin(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        response = start_plex_pin(client)
    assert_is_problem(response, 409, "sign_in_method_unavailable")


def test_an_owner_token_pin_needs_a_session(app: FastAPI) -> None:
    with console_client(app) as client:
        response = client.post(
            f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers()
        )
    assert_is_problem(response, 401, "unauthorized")


def test_an_owner_token_pin_after_setup_needs_a_media_server_administrator(
    app: FastAPI, internet: FakeInternet
) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        # Sign in as the ordinary user instead; their console session is not enough.
        client.cookies.clear()
        signed_in = client.post(
            f"{API}/auth/web/login",
            json={"username": USER_NAME, "password": USER_PASSWORD},
            headers=console_headers(),
        )
        csrf = signed_in.json()["csrf_token"]
        response = client.post(
            f"{API}/auth/plex/pins",
            json={"purpose": "owner_token"},
            headers=console_headers(csrf),
        )
    assert_is_problem(response, 403, "admin_required")
    assert internet.media.users


def test_an_owner_token_pin_is_bound_to_the_session_that_created_it(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        pin_id = client.post(
            f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers(csrf)
        ).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        clock.advance(2)
    with console_client(app) as other:
        other_csrf = claim(other, app)
        response = other.post(
            f"{API}/auth/plex/pins/status",
            json={"pin_id": pin_id},
            headers=console_headers(other_csrf),
        )
    assert_is_problem(response, 410, "pin_expired")


def test_an_unapproved_owner_token_pin_cannot_configure_the_connector(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        pin_id = client.post(
            f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers(csrf)
        ).json()["pin_id"]
        response = client.put(
            f"{API}/setup/media-server",
            json={
                "connector": "media_server",
                "server_type": "plex",
                "url": PLEX_SERVER_URL,
                "plex_pin_id": pin_id,
            },
            headers=console_headers(csrf),
        )
    assert_is_problem(response, 409, "plex_pin_pending")


def test_a_failed_connection_test_leaves_the_owner_token_pin_usable(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        pin_id = client.post(
            f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers(csrf)
        ).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        clock.advance(2)
        assert (
            client.post(
                f"{API}/auth/plex/pins/status",
                json={"pin_id": pin_id},
                headers=console_headers(csrf),
            ).json()["status"]
            == "authorized"
        )
        internet.plex_offline = True
        body: dict[str, Any] = {
            "connector": "media_server",
            "server_type": "plex",
            "url": PLEX_SERVER_URL,
            "plex_pin_id": pin_id,
        }
        failed = client.put(f"{API}/setup/media-server", json=body, headers=console_headers(csrf))
        assert_is_problem(failed, 502, "connector_unreachable")
        internet.plex_offline = False
        assert (
            client.put(
                f"{API}/setup/media-server", json=body, headers=console_headers(csrf)
            ).status_code
            == 200
        )
        # Consumed now that the connector was saved.
        again = client.put(f"{API}/setup/media-server", json=body, headers=console_headers(csrf))
    assert_is_problem(again, 410, "pin_expired")


def test_a_plex_owner_token_from_a_friend_is_refused(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        pin_id = client.post(
            f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers(csrf)
        ).json()["pin_id"]
        internet.plex_tv.add_account(FRIEND_TOKEN, FRIEND_ID, "Robin", owned=False)
        internet.plex_tv.approve(last_plex_code(internet), FRIEND_TOKEN)
        clock.advance(2)
        assert (
            client.post(
                f"{API}/auth/plex/pins/status",
                json={"pin_id": pin_id},
                headers=console_headers(csrf),
            ).json()["status"]
            == "authorized"
        )
        response = client.put(
            f"{API}/setup/media-server",
            json={
                "connector": "media_server",
                "server_type": "plex",
                "url": PLEX_SERVER_URL,
                "plex_pin_id": pin_id,
            },
            headers=console_headers(csrf),
        )
    assert_is_problem(response, 403, "plex_owner_required")


def test_a_plex_sign_in_matches_the_stored_identifier_not_the_resource_name(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        # A resource that calls itself the same but has another identifier.
        internet.plex_tv.add_account(FRIEND_TOKEN, FRIEND_ID, "Robin", machine_id="lookalike")
        internet.plex_tv.resources[FRIEND_TOKEN][0]["name"] = "Home Plex"
        pin_id = start_plex_pin(client).json()["pin_id"]
        internet.plex_tv.approve(last_plex_code(internet), FRIEND_TOKEN)
        clock.advance(2)
        response = client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
    assert_is_problem(response, 403, "not_a_server_user")
    assert MACHINE_ID != "lookalike"


# --- step-up re-authentication --------------------------------------------------------------------


def test_a_password_re_authenticates_the_session(app: FastAPI, clock: FakeClock) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        clock.advance(10 * 60)
        assert client.get(f"{API}/auth/web/session").json()["reauth_expires_at"] is None
        response = client.post(
            f"{API}/auth/web/reauth",
            json={"password": "correct horse"},
            headers=console_headers(csrf),
        )
    assert_matches_contract(f"{API}/auth/web/reauth", "post", response)
    assert response.json()["reauth_expires_at"]


def test_a_wrong_password_does_not_re_authenticate(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.post(
            f"{API}/auth/web/reauth", json={"password": "nope"}, headers=console_headers(csrf)
        )
    assert_is_problem(response, 401, "invalid_credentials")


def test_re_authenticating_as_someone_else_is_refused(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        handle = client.post(
            f"{API}/auth/quick-connect",
            json={"purpose": "reauth"},
            headers=console_headers(csrf),
        ).json()["handle"]
        # Approved by the other user of the same media server: not a step-up.
        internet.media.approve(f"qc-secret-{len(internet.media.quick_connect)}", USER_NAME)
        clock.advance(2)
        response = client.post(
            f"{API}/auth/web/reauth", json={"handle": handle}, headers=console_headers(csrf)
        )
    assert_is_problem(response, 401, "invalid_credentials")


def test_a_reauth_handle_is_bound_to_its_session(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        handle = client.post(
            f"{API}/auth/quick-connect",
            json={"purpose": "reauth"},
            headers=console_headers(csrf),
        ).json()["handle"]
    with console_client(app) as other:
        other_csrf = other.post(
            f"{API}/auth/web/login",
            json={"username": ADMIN_NAME, "password": "correct horse"},
            headers=console_headers(),
        ).json()["csrf_token"]
        response = other.post(
            f"{API}/auth/web/reauth",
            json={"handle": handle},
            headers=console_headers(other_csrf),
        )
    assert_is_problem(response, 410, "quick_connect_expired")


def test_a_reauth_needs_a_web_session(app: FastAPI) -> None:
    with console_client(app) as client:
        set_up_server(client, app)
        client.cookies.clear()
        response = client.post(
            f"{API}/auth/web/reauth", json={"password": "x"}, headers=console_headers("nope")
        )
    assert_is_problem(response, 401, "unauthorized")


def test_a_reauth_handle_cannot_carry_a_pkce_challenge(app: FastAPI) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.post(
            f"{API}/auth/quick-connect",
            json={"purpose": "reauth", "code_challenge": CHALLENGE},
            headers=console_headers(csrf),
        )
    assert_is_problem(response, 400, "validation_error")


def test_a_plex_server_has_no_password_sign_in(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        response = client.post(
            f"{API}/auth/web/login",
            json={"username": ADMIN_NAME, "password": "correct horse"},
            headers=console_headers(),
        )
    assert_is_problem(response, 409, "sign_in_method_unavailable")


def test_a_plex_sign_in_answers_202_until_the_pin_is_approved(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        set_up_plex(client, app, internet, clock)
        pin_id = start_plex_pin(client).json()["pin_id"]
        response = client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
        assert_matches_contract(f"{API}/auth/web/plex/login", "post", response)
        assert response.status_code == 202
        # Asked again within the same second: plex.tv is not called a second time.
        asked = len(internet.plex_tv.requests)
        again = client.post(
            f"{API}/auth/web/plex/login", json={"pin_id": pin_id}, headers=console_headers()
        )
    assert again.status_code == 202
    assert len(internet.plex_tv.requests) == asked


def test_the_pin_status_is_not_checked_more_than_once_a_second(
    app: FastAPI, internet: FakeInternet, clock: FakeClock
) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        pin_id = client.post(
            f"{API}/auth/plex/pins", json={"purpose": "owner_token"}, headers=console_headers(csrf)
        ).json()["pin_id"]
        body = {"pin_id": pin_id}
        assert (
            client.post(
                f"{API}/auth/plex/pins/status", json=body, headers=console_headers(csrf)
            ).json()["status"]
            == "pending"
        )
        internet.plex_tv.approve(last_plex_code(internet), OWNER_TOKEN)
        asked = len(internet.plex_tv.requests)
        # Still "pending": plex.tv was not asked again so soon.
        assert (
            client.post(
                f"{API}/auth/plex/pins/status", json=body, headers=console_headers(csrf)
            ).json()["status"]
            == "pending"
        )
        assert len(internet.plex_tv.requests) == asked
        clock.advance(2)
        assert (
            client.post(
                f"{API}/auth/plex/pins/status", json=body, headers=console_headers(csrf)
            ).json()["status"]
            == "authorized"
        )
