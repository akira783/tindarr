"""Driving the real sign-in flows in a test, the way the console and the app do.

These are the request sequences the tests repeat: claim the server, point it at a fake
media server, sign the first administrator in (which completes setup), then sign in
again as anybody. Everything goes through the HTTP API and the real adapters, so a test
that uses them exercises the whole path.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support.console import claim, console_headers
from tests.support.upstream import ADMIN_API_KEY, MEDIA_SERVER_URL, FakeInternet

API = "/api/v1"
ADMIN_NAME = "alex"
ADMIN_PASSWORD = "correct horse"
USER_NAME = "robin"
USER_PASSWORD = "another one"
APP_DEVICE: dict[str, str] = {"name": "Pixel 9", "platform": "android", "app_version": "0.1.0"}


def configure_media_server(client: TestClient, csrf: str | None, **fields: Any) -> Any:
    """Send the wizard's media server step, pointing at the fake media server."""
    body: dict[str, Any] = {
        "connector": "media_server",
        "server_type": "jellyfin",
        "url": MEDIA_SERVER_URL,
        "api_key": ADMIN_API_KEY,
    } | fields
    return client.put(f"{API}/setup/media-server", json=body, headers=console_headers(csrf))


def web_login(
    client: TestClient,
    username: str = ADMIN_NAME,
    password: str = ADMIN_PASSWORD,
    **headers: Any,
) -> Any:
    """Sign the console in with a password."""
    return client.post(
        f"{API}/auth/web/login",
        json={"username": username, "password": password},
        headers=console_headers(**headers),
    )


def app_login(
    client: TestClient, username: str = ADMIN_NAME, password: str = ADMIN_PASSWORD
) -> Any:
    """Sign the app in with a password."""
    return client.post(
        f"{API}/auth/login",
        json={"username": username, "password": password, "device": APP_DEVICE},
    )


def set_up_server(client: TestClient, app: FastAPI, **fields: Any) -> str:
    """Claim, configure the media server and sign the first administrator in.

    Returns the CSRF token of the web session that completed setup; its cookie is left
    in the client's jar, so the next requests are a signed-in administrator's.
    """
    csrf = claim(client, app)
    response = configure_media_server(client, csrf, **fields)
    assert response.status_code == 200, response.text
    signed_in = web_login(client)
    assert signed_in.status_code == 200, signed_in.text
    assert signed_in.json()["setup_completed_now"] is True
    token: str = signed_in.json()["csrf_token"]
    return token


def seed_jellyfin(internet: FakeInternet) -> FakeInternet:
    """Give the fake media server an administrator and an ordinary user."""
    internet.media.add_user(ADMIN_NAME, ADMIN_PASSWORD, admin=True)
    internet.media.add_user(
        USER_NAME, USER_PASSWORD, user_id="b" * 32, admin=False, remote_access=True
    )
    return internet
