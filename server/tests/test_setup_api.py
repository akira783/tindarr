"""The setup endpoints over HTTP: cookies, CSRF, locks and problems (auth.md, §2 and §3)."""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.support import (
    CONSOLE_ORIGIN,
    FakeClock,
    FakeMediaServer,
    FakeMediaServers,
    build_app,
    claim,
    complete_setup,
    configure_media_server,
    console_client,
    console_headers,
    cookies_of,
    services_of,
    setup_code,
)
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.adapters.emby import EmbyServer
from tindarr.adapters.factory import media_server_factory
from tindarr.adapters.jellyfin import JellyfinServer
from tindarr.adapters.plex import PlexServer
from tindarr.api.cookies import PLAIN_NAMES, SECURE_NAMES
from tindarr.ports.media_server import MediaServerConnection, MediaServerKind

CLAIM_PATH = "/api/v1/setup/claim"
STATE_PATH = "/api/v1/setup/state"
MEDIA_SERVER_PATH = "/api/v1/setup/media-server"


@pytest.fixture
def media_servers() -> FakeMediaServers:
    return FakeMediaServers()


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, media_servers: FakeMediaServers) -> Any:
    return build_app(data_dir, clock=clock, media_servers=media_servers)


# --- the claim ----------------------------------------------------------------------


def test_claiming_sets_a_host_only_session_cookie(app: Any) -> None:
    with console_client(app) as client:
        response = client.post(
            CLAIM_PATH, json={"setup_code": setup_code(app)}, headers=console_headers()
        )

    assert_matches_contract(CLAIM_PATH, "post", response)
    body = response.json()
    assert body["kind"] == "setup"
    assert body["user"] is None
    assert body["csrf_token"]
    cookie = cookies_of(response)[SECURE_NAMES.setup]
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie.replace("samesite", "SameSite")
    assert "Path=/" in cookie
    assert "Max-Age" not in cookie
    assert "Domain" not in cookie
    # The session token is only in the cookie, never in the body.
    assert client.cookies[SECURE_NAMES.setup] not in response.text


def test_a_wrong_code_is_refused(app: Any) -> None:
    with console_client(app) as client:
        response = client.post(
            CLAIM_PATH, json={"setup_code": "00000000000Z"}, headers=console_headers()
        )
    assert_is_problem(response, 401, "invalid_setup_code")
    assert SECURE_NAMES.setup not in cookies_of(response)


def test_a_second_claim_revokes_the_first(app: Any) -> None:
    with console_client(app) as first, console_client(app) as second:
        first_csrf = claim(first, app)
        claim(second, app)
        answer = first.get("/api/v1/auth/web/session")
    assert_is_problem(answer, 401, "unauthorized")
    assert first_csrf


def test_the_claim_refuses_a_bearer_token(app: Any) -> None:
    with console_client(app) as client:
        response = client.post(
            CLAIM_PATH,
            json={"setup_code": setup_code(app)},
            headers=console_headers() | {"Authorization": "Bearer something"},
        )
    assert_is_problem(response, 401, "unauthorized")


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null", "http://console.test"])
def test_the_claim_checks_the_origin(app: Any, origin: str | None) -> None:
    headers = {} if origin is None else {"Origin": origin}
    with console_client(app) as client:
        response = client.post(CLAIM_PATH, json={"setup_code": setup_code(app)}, headers=headers)
    assert_is_problem(response, 403, "csrf_failed")


def test_the_claim_refuses_plain_http_from_the_network(data_dir: Path, clock: FakeClock) -> None:
    app = build_app(data_dir, clock=clock)
    with console_client(app, scheme="http") as client:
        response = client.post(
            CLAIM_PATH,
            json={"setup_code": setup_code(app)},
            headers={"Origin": "http://console.test"},
        )
    assert_is_problem(response, 403, "https_required")


def test_plain_http_works_from_loopback_to_localhost(data_dir: Path, clock: FakeClock) -> None:
    app = build_app(data_dir, clock=clock)
    with console_client(app, scheme="http", host="localhost", peer="127.0.0.1") as client:
        response = client.post(
            CLAIM_PATH,
            json={"setup_code": setup_code(app)},
            headers={"Origin": "http://localhost"},
        )
    assert response.status_code == 200
    assert "Secure" in cookies_of(response)[SECURE_NAMES.setup]


def test_the_plain_http_opt_in_drops_the_prefix_and_secure(
    data_dir: Path, clock: FakeClock
) -> None:
    app = build_app(data_dir, clock=clock, allow_http_console=True)
    with console_client(app, scheme="http", peer="192.168.1.20") as client:
        response = client.post(
            CLAIM_PATH,
            json={"setup_code": setup_code(app)},
            headers={"Origin": "http://console.test"},
        )
        assert response.status_code == 200
        cookie = cookies_of(response)[PLAIN_NAMES.setup]
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie
        # The unprefixed cookie is read on such a request, and the session works.
        assert client.get("/api/v1/setup/state").status_code == 200


def test_the_plain_http_opt_in_does_not_apply_from_the_internet(
    data_dir: Path, clock: FakeClock
) -> None:
    app = build_app(data_dir, clock=clock, allow_http_console=True)
    with console_client(app, scheme="http", peer="203.0.113.7") as client:
        response = client.post(
            CLAIM_PATH,
            json={"setup_code": setup_code(app)},
            headers={"Origin": "http://console.test"},
        )
    assert_is_problem(response, 403, "https_required")


def test_failed_claims_are_rate_limited_per_address(app: Any) -> None:
    with console_client(app) as client:
        for _ in range(5):
            client.post(CLAIM_PATH, json={"setup_code": "00000000000Z"}, headers=console_headers())
        response = client.post(
            CLAIM_PATH, json={"setup_code": "00000000000Z"}, headers=console_headers()
        )
    assert_is_problem(response, 429, "rate_limited")
    assert response.json()["retry_after_ms"] > 0
    assert int(response.headers["retry-after"]) > 0
    assert_matches_contract(CLAIM_PATH, "post", response)


def test_claiming_a_server_already_set_up(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        complete_setup(client, app)
        response = client.post(
            CLAIM_PATH, json={"setup_code": "00000000000Z"}, headers=console_headers()
        )
    assert_is_problem(response, 409, "setup_completed")
    assert_matches_contract(CLAIM_PATH, "post", response)


@pytest.mark.parametrize("code", ["", "short", "x" * 65])
def test_a_malformed_code_is_a_validation_error(app: Any, code: str) -> None:
    with console_client(app) as client:
        response = client.post(CLAIM_PATH, json={"setup_code": code}, headers=console_headers())
    assert_is_problem(response, 400, "validation_error")
    assert code not in response.text or not code


def test_a_csrf_token_that_is_not_ascii_is_a_csrf_failure(app: Any) -> None:
    with console_client(app) as client:
        claim(client, app)
        response = client.put(
            MEDIA_SERVER_PATH,
            json={"connector": "media_server", "server_type": "jellyfin", "url": "http://j.lan"},
            headers={b"Origin": CONSOLE_ORIGIN.encode(), b"X-CSRF-Token": b"caf\xe9"},
        )
    assert_is_problem(response, 403, "csrf_failed")


def test_a_bearer_token_that_is_not_ascii_is_unauthorized(app: Any) -> None:
    with console_client(app) as client:
        response = client.get("/api/v1/setup/state", headers={b"Authorization": b"Bearer caf\xe9"})
    assert_is_problem(response, 401, "unauthorized")


def response_text(body: object) -> str:
    """The JSON body as text, to assert a secret is nowhere in it."""
    return json.dumps(body)


# --- the wizard's state -------------------------------------------------------------


def test_the_state_needs_the_setup_session(app: Any) -> None:
    with console_client(app) as client:
        assert_is_problem(client.get(STATE_PATH), 401, "unauthorized")
        claim(client, app)
        response = client.get(STATE_PATH)
    assert_matches_contract(STATE_PATH, "get", response)
    assert response.json() == {
        "media_server": None,
        "media_server_locked": False,
        "locked_fields": [],
        "locked_values": {"server_type": None, "url": None, "verify_tls": None},
        "auth_methods": [],
    }


def test_the_state_refuses_a_bearer_token(app: Any) -> None:
    with console_client(app) as client:
        claim(client, app)
        response = client.get(STATE_PATH, headers={"Authorization": "Bearer something"})
    assert_is_problem(response, 401, "unauthorized")


def test_the_state_shows_the_configured_media_server(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        configure_media_server(client, csrf)
        response = client.get(STATE_PATH)
    # The name comes from the connection test, so the wizard can say what it reached.
    assert response.json()["media_server"] == {"kind": "jellyfin", "name": "Home Jellyfin"}
    assert response.json()["auth_methods"] == ["password"]
    assert_matches_contract(STATE_PATH, "get", response)


def test_the_state_lists_the_fields_the_environment_locks(
    data_dir: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_MEDIA_SERVER_KIND", "emby")
    monkeypatch.setenv("TINDARR_MEDIA_SERVER_URL", "http://emby.lan:8096")
    monkeypatch.setenv("TINDARR_MEDIA_SERVER_API_KEY", "locked-key")
    app = build_app(data_dir, clock=clock)
    with console_client(app) as client:
        claim(client, app)
        body = client.get(STATE_PATH).json()
    assert body["media_server_locked"] is True
    assert sorted(body["locked_fields"]) == ["api_key", "server_type", "url"]
    assert body["media_server"] == {"kind": "emby", "name": None}
    # The wizard can show and resend the locked values; the API key is never returned.
    assert body["locked_values"] == {
        "server_type": "emby",
        "url": "http://emby.lan:8096",
        "verify_tls": None,
    }
    assert "locked-key" not in response_text(body)


# --- the media server step ----------------------------------------------------------


def test_configuring_the_media_server(app: Any, media_servers: FakeMediaServers) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        response = configure_media_server(client, csrf)

    assert_matches_contract(MEDIA_SERVER_PATH, "put", response)
    body = response.json()
    assert body["health"] == "ok"
    assert body["server_name"] == "Home Jellyfin"
    assert media_servers.last.url == "http://jellyfin.lan:8096"
    assert media_servers.last.verify_tls is True


def test_configuring_needs_the_csrf_token_and_the_origin(app: Any) -> None:
    with console_client(app) as client:
        claim(client, app)
        missing = configure_media_server(client, None)
        wrong = configure_media_server(client, "not-the-token")
        response = client.put(
            MEDIA_SERVER_PATH,
            json={
                "connector": "media_server",
                "server_type": "jellyfin",
                "url": "http://jellyfin.lan:8096",
                "api_key": "k",
            },
            headers={"X-CSRF-Token": "whatever"},
        )
    assert_is_problem(missing, 403, "csrf_failed")
    assert_is_problem(wrong, 403, "csrf_failed")
    assert_is_problem(response, 403, "csrf_failed")


def test_configuring_needs_the_setup_session(app: Any) -> None:
    with console_client(app) as client:
        response = configure_media_server(client, "no-session")
    assert_is_problem(response, 401, "unauthorized")


def test_a_failed_connection_test_is_a_bad_gateway(
    app: Any, media_servers: FakeMediaServers
) -> None:
    media_servers.server = FakeMediaServer(health="unreachable")
    with console_client(app) as client:
        csrf = claim(client, app)
        response = configure_media_server(client, csrf)
    assert_is_problem(response, 502, "connector_unreachable")
    assert_matches_contract(MEDIA_SERVER_PATH, "put", response)


def test_a_locked_field_cannot_be_given_another_value(
    data_dir: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_MEDIA_SERVER_URL", "http://jellyfin.lan:8096")
    app = build_app(data_dir, clock=clock)
    with console_client(app) as client:
        csrf = claim(client, app)
        refused = configure_media_server(client, csrf, url="http://evil.example")
        accepted = configure_media_server(client, csrf)
    assert_is_problem(refused, 409, "setting_locked")
    assert accepted.status_code == 200
    assert_matches_contract(MEDIA_SERVER_PATH, "put", refused)


def test_the_stored_secret_is_never_sent_to_a_new_address(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        kept = client.put(
            MEDIA_SERVER_PATH,
            json={
                "connector": "media_server",
                "server_type": "jellyfin",
                "url": "http://jellyfin.lan:8096",
            },
            headers=console_headers(csrf),
        )
        moved = client.put(
            MEDIA_SERVER_PATH,
            json={
                "connector": "media_server",
                "server_type": "jellyfin",
                "url": "http://somewhere-else.lan:8096",
            },
            headers=console_headers(csrf),
        )
    assert kept.status_code == 200
    assert_is_problem(moved, 409, "secret_required")


@pytest.mark.parametrize(
    "body",
    [
        {
            "connector": "media_server",
            "server_type": "plex",
            "url": "http://plex.lan:32400",
            "api_key": "nope",
        },
        {
            "connector": "media_server",
            "server_type": "jellyfin",
            "url": "http://j.lan",
            "plex_pin_id": "aaaaaaaaaaaaaaaaaaaaaa",
        },
        {"connector": "media_server", "server_type": "kodi", "url": "http://k.lan"},
        {"connector": "media_server", "server_type": "jellyfin", "url": "ftp://j.lan"},
        {"connector": "llm", "server_type": "jellyfin", "url": "http://j.lan"},
    ],
)
def test_an_impossible_configuration_is_a_validation_error(app: Any, body: dict[str, Any]) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        response = client.put(MEDIA_SERVER_PATH, json=body, headers=console_headers(csrf))
    assert_is_problem(response, 400, "validation_error")


def test_a_plex_pin_handle_is_unknown_until_step_two_b(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        response = client.put(
            MEDIA_SERVER_PATH,
            json={
                "connector": "media_server",
                "server_type": "plex",
                "url": "http://plex.lan:32400",
                "plex_pin_id": "a" * 22,
            },
            headers=console_headers(csrf),
        )
    assert_is_problem(response, 410, "pin_expired")


@pytest.mark.parametrize(
    ("kind", "adapter"),
    [("jellyfin", JellyfinServer), ("emby", EmbyServer), ("plex", PlexServer)],
)
def test_the_default_wiring_builds_the_real_adapters(kind: MediaServerKind, adapter: type) -> None:
    # No network here: only which class the composition root would talk through.
    built = media_server_factory()(
        MediaServerConnection(kind=kind, url="http://media.lan", secret="s")
    )
    assert isinstance(built, adapter)


def test_connection_tests_are_limited_per_session(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        for _ in range(10):
            assert configure_media_server(client, csrf).status_code == 200
        response = configure_media_server(client, csrf)
    assert_is_problem(response, 429, "rate_limited")


# --- completion ---------------------------------------------------------------------


def test_completing_setup_opens_a_web_session_the_console_can_use(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        assert configure_media_server(client, csrf).status_code == 200
        session_csrf = complete_setup(client, app)

        session = client.get("/api/v1/auth/web/session")
        info = client.get("/api/v1/server/info")

    assert session.status_code == 200
    body = session.json()
    assert body["kind"] == "web"
    assert body["csrf_token"] == session_csrf
    assert body["csrf_token"] != csrf
    assert body["user"]["role"] == "admin"
    assert body["reauth_expires_at"] is not None
    assert info.json()["setup_required"] is False
    assert info.json()["auth_methods"] == ["password"]


def test_the_setup_code_file_is_gone_after_completion(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        configure_media_server(client, csrf)
        complete_setup(client, app)
        assert not services_of(app).setup.code_path.exists()


def test_the_setup_session_cannot_reach_the_console(app: Any) -> None:
    with console_client(app) as client:
        claim(client, app)
        # A setup cookie is not a web session: it never reaches a signed-in endpoint.
        response = client.post("/api/v1/auth/logout", headers=console_headers("whatever"))
    assert_is_problem(response, 401, "unauthorized")


def test_the_origin_of_another_host_is_refused_even_with_a_valid_session(app: Any) -> None:
    with console_client(app) as client:
        csrf = claim(client, app)
        response = configure_media_server(client, csrf)
        assert response.status_code == 200
        forged = client.put(
            MEDIA_SERVER_PATH,
            json={
                "connector": "media_server",
                "server_type": "jellyfin",
                "url": "http://jellyfin.lan:8096",
                "api_key": "admin-api-key",
            },
            headers=console_headers(csrf, origin="https://evil.example"),
        )
    assert_is_problem(forged, 403, "csrf_failed")


def test_no_secret_ever_reaches_the_logs(app: Any, log_stream: io.StringIO) -> None:
    with console_client(app) as client:
        code = setup_code(app)
        csrf = claim(client, app)
        configure_media_server(client, csrf, api_key="super-secret-api-key")
        token = client.cookies[SECURE_NAMES.setup]
        session_csrf = complete_setup(client, app)
        client.get("/api/v1/auth/web/session")
    text = log_stream.getvalue()
    assert text  # the requests were logged
    for secret in (code, csrf, token, session_csrf, "super-secret-api-key"):
        assert secret not in text


def test_the_console_client_sends_what_the_console_sends() -> None:
    assert console_headers("token") == {"Origin": CONSOLE_ORIGIN, "X-CSRF-Token": "token"}


def test_claiming_twice_from_one_browser_keeps_the_newest_session(app: Any) -> None:
    with console_client(app) as client:
        first = claim(client, app)
        second = claim(client, app)
        state = client.get(STATE_PATH)
    assert first != second
    assert state.status_code == 200


def assert_cookie_is_cleared(client: TestClient, name: str) -> None:
    assert client.cookies.get(name) is None
