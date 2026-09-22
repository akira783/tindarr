"""Connecting a phone, end to end (docs/auth.md, section 9; check (a) of the roadmap).

Covers what the roadmap asks of pairing: expiry, single use, a preview that reveals
nothing for an invalid code, no tokens before approval, rejection, a wrong verifier, and
the rate limits — plus the console's half, which is cookie-only.
"""

import base64
import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    API,
    APP_DEVICE,
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
from tindarr.auth.pairing import MAX_OPEN_PAIRINGS, PAIRING_LIFETIME, confirmation_code

PAIRINGS = f"{API}/pairings"
PREVIEW = f"{API}/auth/pair/preview"
PAIR = f"{API}/auth/pair"
COMPLETE = f"{API}/auth/pair/complete"
PUBLIC_URL = "https://tindarr.example.com"
VERIFIER = "x" * 43
OTHER_VERIFIER = "y" * 43


def challenge_of(verifier: str) -> str:
    """The PKCE S256 challenge of a verifier, as the app computes it."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


CHALLENGE = challenge_of(VERIFIER)


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet, monkeypatch: Any) -> FastAPI:
    # public_url from the environment: pairing needs one, and setting it from the
    # console is the subject of test_public_url.py.
    monkeypatch.setenv("TINDARR_PUBLIC_URL", PUBLIC_URL)
    return build_app(data_dir, clock=clock, internet=internet)


@pytest.fixture
def console(app: FastAPI) -> Any:
    """A signed-in console, with its CSRF token."""
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        yield client, csrf


def create(client: TestClient, csrf: str) -> Any:
    return client.post(PAIRINGS, headers=console_headers(csrf))


def request_pairing(client: TestClient, code: str, challenge: str = CHALLENGE) -> Any:
    return client.post(PAIR, json={"code": code, "device": APP_DEVICE, "code_challenge": challenge})


def complete(client: TestClient, code: str, verifier: str = VERIFIER) -> Any:
    return client.post(COMPLETE, json={"code": code, "code_verifier": verifier})


# --- creating a pairing ----------------------------------------------------------------


def test_creating_a_pairing_returns_the_code_and_the_link_once(console: Any) -> None:
    client, csrf = console
    response = create(client, csrf)
    assert response.status_code == 201
    assert_matches_contract(PAIRINGS, "post", response)
    body = response.json()
    assert body["status"] == "pending"
    assert len(body["code"]) >= 22
    assert body["link"] == (
        f"tindarr://pair?server=https%3A%2F%2Ftindarr.example.com&code={body['code']}"
    )
    assert body["retry_after_ms"] == 2000
    # Reading it back never shows the code again.
    again = client.get(f"{PAIRINGS}/{body['id']}")
    assert_matches_contract(f"{PAIRINGS}/{{pairing_id}}", "get", again)
    assert "code" not in again.json()


def test_creating_a_pairing_needs_a_public_url(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    app = build_app(data_dir, clock=clock, internet=internet)
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert_is_problem(create(client, csrf), 409, "public_url_not_set")


def test_a_bearer_token_cannot_create_a_pairing(console: Any, app: FastAPI) -> None:
    client, _ = console
    # A token stolen from a phone must not be able to connect another phone.
    response = client.post(PAIRINGS, headers={"Authorization": "Bearer something"})
    assert_is_problem(response, 401, "unauthorized")


def test_creating_a_pairing_needs_the_csrf_token(console: Any) -> None:
    client, _ = console
    assert_is_problem(client.post(PAIRINGS, headers={"Origin": CONSOLE_ORIGIN}), 403, "csrf_failed")


def test_a_user_may_only_have_three_pending_pairings(console: Any) -> None:
    client, csrf = console
    for _ in range(MAX_OPEN_PAIRINGS):
        assert create(client, csrf).status_code == 201
    refused = create(client, csrf)
    assert_is_problem(refused, 429, "rate_limited")
    assert refused.json()["retry_after_ms"] > 0
    assert refused.headers["Retry-After"] == "300"


def test_the_cap_frees_itself_when_a_pairing_expires(console: Any, clock: FakeClock) -> None:
    client, csrf = console
    for _ in range(MAX_OPEN_PAIRINGS):
        assert create(client, csrf).status_code == 201
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    assert create(client, csrf).status_code == 201


# --- the preview -----------------------------------------------------------------------


def test_the_preview_shows_only_the_names_and_the_deadline(console: Any) -> None:
    client, csrf = console
    code = create(client, csrf).json()["code"]
    response = client.post(PREVIEW, json={"code": code})
    assert response.status_code == 200
    assert_matches_contract(PREVIEW, "post", response)
    assert set(response.json()) == {"server_name", "user_name", "expires_at"}
    assert response.json()["user_name"] == "alex"


@pytest.mark.parametrize("code", ["a" * 22, "Z-_0123456789abcdefghi"])
def test_an_invalid_code_reveals_nothing(console: Any, code: str) -> None:
    client, _ = console
    assert_is_problem(client.post(PREVIEW, json={"code": code}), 410, "pairing_expired")


def test_the_preview_stops_working_once_the_code_was_used(console: Any) -> None:
    client, csrf = console
    code = create(client, csrf).json()["code"]
    assert request_pairing(client, code).status_code == 202
    assert_is_problem(client.post(PREVIEW, json={"code": code}), 410, "pairing_expired")


def test_the_preview_stops_working_when_the_pairing_expires(console: Any, clock: FakeClock) -> None:
    client, csrf = console
    code = create(client, csrf).json()["code"]
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    assert_is_problem(client.post(PREVIEW, json={"code": code}), 410, "pairing_expired")


# --- the phone's request ---------------------------------------------------------------


def test_requesting_returns_the_confirmation_code_both_screens_show(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    response = request_pairing(client, created["code"])
    assert response.status_code == 202
    assert_matches_contract(PAIR, "post", response)
    body = response.json()
    assert body["pending"] is True
    assert body["confirmation_code"] == confirmation_code(CHALLENGE)
    assert len(body["confirmation_code"]) == 4
    assert response.headers["Retry-After"] == "2"
    # The console shows the same four digits, with the device that asked.
    pairing = client.get(f"{PAIRINGS}/{created['id']}").json()
    assert pairing["status"] == "awaiting_approval"
    assert pairing["confirmation_code"] == body["confirmation_code"]
    assert pairing["device"] == {"name": "Pixel 9", "platform": "android", "app_version": "0.1.0"}
    assert pairing["requested_from"] == "192.168.1.20"


def test_a_code_can_only_be_requested_once(console: Any) -> None:
    client, csrf = console
    code = create(client, csrf).json()["code"]
    assert request_pairing(client, code).status_code == 202
    assert_is_problem(
        request_pairing(client, code, challenge_of(OTHER_VERIFIER)), 410, "pairing_expired"
    )


# --- completing ------------------------------------------------------------------------


def test_no_tokens_before_the_console_approves(console: Any) -> None:
    client, csrf = console
    code = create(client, csrf).json()["code"]
    request_pairing(client, code)
    response = complete(client, code)
    assert response.status_code == 202
    assert_matches_contract(COMPLETE, "post", response)
    assert "access_token" not in response.text
    assert response.json()["retry_after_ms"] == 2000


def test_the_whole_flow(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    approved = client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    assert approved.status_code == 200
    assert_matches_contract(f"{PAIRINGS}/{{pairing_id}}/approve", "post", approved)
    assert approved.json()["status"] == "approved"

    response = complete(client, created["code"])
    assert response.status_code == 200
    assert_matches_contract(COMPLETE, "post", response)
    tokens = response.json()
    assert tokens["user"]["name"] == "alex"

    # The console then sees the connected phone and can revoke its session.
    pairing = client.get(f"{PAIRINGS}/{created['id']}").json()
    assert pairing["status"] == "completed"
    assert pairing["retry_after_ms"] is None
    assert pairing["session_id"] is not None

    # The token really works, until the pairing is revoked.
    me = client.get(f"{API}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert (
        client.delete(f"{PAIRINGS}/{created['id']}", headers=console_headers(csrf)).status_code
        == 204
    )
    after = client.get(f"{API}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert_is_problem(after, 401, "unauthorized")


def test_a_code_completes_only_once(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    assert complete(client, created["code"]).status_code == 200
    assert_is_problem(complete(client, created["code"]), 410, "pairing_expired")


def test_a_wrong_verifier_never_gets_the_tokens(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    assert_is_problem(complete(client, created["code"], OTHER_VERIFIER), 410, "pairing_expired")
    # And the right one still works: a wrong guess consumes nothing.
    assert complete(client, created["code"]).status_code == 200


def test_a_rejected_pairing_tells_the_phone_to_stop(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    assert (
        client.delete(f"{PAIRINGS}/{created['id']}", headers=console_headers(csrf)).status_code
        == 204
    )
    assert_is_problem(complete(client, created["code"]), 403, "pairing_rejected")
    # Without the verifier, even that much is not revealed.
    assert_is_problem(complete(client, created["code"], OTHER_VERIFIER), 410, "pairing_expired")


def test_a_cancelled_pairing_that_no_phone_asked_for_is_just_gone(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    client.delete(f"{PAIRINGS}/{created['id']}", headers=console_headers(csrf))
    assert_is_problem(complete(client, created["code"]), 410, "pairing_expired")


def test_an_expired_pairing_gives_no_tokens(console: Any, clock: FakeClock) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    assert_is_problem(complete(client, created["code"]), 410, "pairing_expired")
    assert client.get(f"{PAIRINGS}/{created['id']}").json()["status"] == "expired"


def csrf_of(client: TestClient) -> str:
    """The CSRF token of the session the client currently holds."""
    token: str = client.get(f"{API}/auth/web/session").json()["csrf_token"]
    return token


def test_completing_applies_the_remote_access_rule(app: FastAPI, internet: FakeInternet) -> None:
    # A user their media server keeps on the local network does not get a token pair
    # from outside it, even with a pairing their own console approved.
    internet.media.add_user("sam", "pw", user_id="c" * 32, admin=False, remote_access=False)
    with console_client(app) as client:
        set_up_server(client, app)
        assert web_login(client, "sam", "pw").status_code == 200
        created = create(client, csrf_of(client)).json()
        request_pairing(client, created["code"])
        approve = client.post(
            f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf_of(client))
        )
        assert approve.status_code == 200
    with console_client(app, peer="203.0.113.9") as public:
        assert_is_problem(complete(public, created["code"]), 403, "remote_access_denied")


def test_completing_refuses_an_account_disabled_since(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        assert web_login(client, "robin", "another one").status_code == 200
        created = create(client, csrf_of(client)).json()
        request_pairing(client, created["code"])
        client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf_of(client)))
        # The administrator disables them between the approval and the phone's poll.
        admin = web_login(client, "alex", "correct horse")
        assert admin.status_code == 200
        robin = next(
            user
            for user in client.get(f"{API}/admin/users").json()["users"]
            if user["name"] == "robin"
        )
        disabled = client.patch(
            f"{API}/admin/users/{robin['id']}",
            json={"enabled": False},
            headers=console_headers(admin.json()["csrf_token"]),
        )
        assert disabled.status_code == 200
        assert_is_problem(complete(client, created["code"]), 403, "account_disabled")
    del csrf


# --- the console's half ------------------------------------------------------------------


def test_another_users_pairing_is_not_found(app: FastAPI, internet: FakeInternet) -> None:
    with console_client(app) as owner:
        csrf = set_up_server(owner, app)
        created = create(owner, csrf).json()
    with console_client(app) as other:
        assert web_login(other, "robin", "another one").status_code == 200
        token = csrf_of(other)
        assert_is_problem(other.get(f"{PAIRINGS}/{created['id']}"), 404, "not_found")
        assert_is_problem(
            other.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(token)),
            404,
            "not_found",
        )


def test_approving_before_a_phone_asked_is_refused(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    refused = client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    assert_is_problem(refused, 409, "pairing_not_awaiting_approval")


def test_approving_twice_is_refused(console: Any) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    assert (
        client.post(
            f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf)
        ).status_code
        == 200
    )
    refused = client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    assert_is_problem(refused, 409, "pairing_not_awaiting_approval")


def test_approving_an_expired_pairing_is_refused(console: Any, clock: FakeClock) -> None:
    client, csrf = console
    created = create(client, csrf).json()
    request_pairing(client, created["code"])
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    refused = client.post(f"{PAIRINGS}/{created['id']}/approve", headers=console_headers(csrf))
    assert_is_problem(refused, 410, "pairing_expired")


# --- rate limits -------------------------------------------------------------------------


def test_the_three_public_calls_share_one_per_ip_limit(console: Any) -> None:
    client, _ = console
    limit = 10
    for _ in range(limit):
        client.post(PREVIEW, json={"code": "a" * 22})
    refused = client.post(PREVIEW, json={"code": "a" * 22})
    assert_is_problem(refused, 429, "rate_limited")
    assert refused.json()["retry_after_ms"] > 0
    assert "Retry-After" in refused.headers
