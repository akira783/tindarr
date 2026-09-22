"""Building apps and clients that behave like the browsers and phones the server serves.

``server_config`` accepts the host names the test clients send, and ``console_client``
speaks like the web console: HTTPS, a private client address, an ``Origin`` header and
the CSRF token of the session it holds.
"""

from collections.abc import Awaitable, Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support.fakes import FakeClock, FakeMediaServers, media_user
from tindeerr.api.cookies import SECURE_NAMES
from tindeerr.api.deps import AppServices
from tindeerr.auth.setupcode import read_setup_code
from tindeerr.core.config import ServerConfig
from tindeerr.main.app import Wiring, create_app
from tindeerr.ports.media_server import MediaUser
from tindeerr.storage.sessions import Device

#: The host the console is opened at in the tests, and the one TestClient sends by default.
CONSOLE_HOST = "console.test"
TEST_HOST = "testserver"
CONSOLE_ORIGIN = f"https://{CONSOLE_HOST}"
#: A private address, as a browser on the household's network has.
CONSOLE_PEER = "192.168.1.20"


def server_config(data_dir: Path, **overrides: Any) -> ServerConfig:
    """A configuration that answers to the host names the test clients use."""
    values: dict[str, Any] = {
        "data_dir": data_dir,
        "allowed_hosts": (CONSOLE_HOST, TEST_HOST),
    }
    return ServerConfig.model_validate(values | overrides)


def build_app(
    data_dir: Path,
    *,
    clock: FakeClock | None = None,
    media_servers: FakeMediaServers | None = None,
    **overrides: Any,
) -> FastAPI:
    """Build the application with the test's clock and fake media server."""
    wiring = Wiring(
        clock=clock or FakeClock(),
        media_servers=media_servers or FakeMediaServers(),
    )
    return create_app(server_config(data_dir, **overrides), wiring)


@contextmanager
def console_client(
    app: FastAPI, *, peer: str = CONSOLE_PEER, host: str = CONSOLE_HOST, scheme: str = "https"
) -> Generator[TestClient]:
    """A client that reaches the console at ``https://console.test`` from the local network."""
    with TestClient(app, base_url=f"{scheme}://{host}", client=(peer, 54321)) as client:
        yield client


def console_headers(csrf_token: str | None = None, origin: str = CONSOLE_ORIGIN) -> dict[str, str]:
    """The headers a console request carries: its ``Origin`` and, if any, its CSRF token."""
    headers = {"Origin": origin}
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    return headers


def cookies_of(response: object) -> Mapping[str, str]:
    """The cookies a response set, by name (the raw ``Set-Cookie`` values)."""
    headers: Any = getattr(response, "headers", None)
    values: list[str] = [] if headers is None else headers.get_list("set-cookie")
    return {value.split("=", 1)[0]: value for value in values}


def run[T](client: TestClient, call: Callable[[], Awaitable[T]]) -> T:
    """Run a coroutine in the application's event loop (to call a service directly)."""
    # anyio's blocking portal is loosely typed; the coroutine's own type is what matters.
    portal: Any = cast("Any", client).portal
    assert portal is not None, "use console_client() so the application is running"
    return cast("T", portal.call(call))


def services_of(app: FastAPI) -> AppServices:
    """The services the running application built."""
    services: Any = app.state.services
    assert isinstance(services, AppServices)
    return services


def setup_code(app: FastAPI) -> str:
    """Read the setup code the server wrote for its first run."""
    code = read_setup_code(services_of(app).setup.code_path)
    assert code is not None
    return code


def claim(client: TestClient, app: FastAPI) -> str:
    """Claim the server as the console does; return the setup session's CSRF token."""
    response = client.post(
        "/api/v1/setup/claim",
        json={"setup_code": setup_code(app)},
        headers=console_headers(),
    )
    assert response.status_code == 200, response.text
    csrf: str = response.json()["csrf_token"]
    return csrf


def configure_media_server(client: TestClient, csrf_token: str | None, **fields: Any) -> Any:
    """Send the wizard's media server step with the fake adapter's defaults."""
    body: dict[str, Any] = {
        "connector": "media_server",
        "server_type": "jellyfin",
        "url": "http://jellyfin.lan:8096",
        "api_key": "admin-api-key",
    } | fields
    return client.put("/api/v1/setup/media-server", json=body, headers=console_headers(csrf_token))


def complete_setup(client: TestClient, app: FastAPI, user: MediaUser | None = None) -> str:
    """Finish first-run setup as the web sign-in of step 2b will, and keep the cookie.

    Returns the web session's CSRF token; the session cookie is left in the client's
    cookie jar, so the next requests are those of a signed-in administrator.
    """
    services = services_of(app)
    setup_token = client.cookies[SECURE_NAMES.setup]
    authenticated = run(
        client, lambda: services.sessions.authenticate_cookie(setup_token, ("setup",))
    )
    completed = run(
        client,
        lambda: services.setup.complete_setup(
            authenticated.session,
            user or media_user(),
            device=Device("Firefox on Linux", "web"),
            client_is_private=True,
        ),
    )
    del client.cookies[SECURE_NAMES.setup]
    client.cookies.set(SECURE_NAMES.session, completed.grant.token)
    return completed.grant.csrf_token


def sign_in_console(client: TestClient, app: FastAPI, user: MediaUser | None = None) -> str:
    """Claim, configure and complete setup; return the web session's CSRF token."""
    csrf = claim(client, app)
    assert configure_media_server(client, csrf).status_code == 200
    return complete_setup(client, app, user)
