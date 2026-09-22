"""Fake Jellyfin, Emby and plex.tv servers, behind ``httpx2.MockTransport``.

The tests drive the **real** adapters against these: no network, no container, and the
wire details (the ``Authorization: MediaBrowser …`` header, the Quick Connect secret in
a query string, plex.tv's XML endpoints) are exercised exactly as in production.

Each fake keeps the state a real server would — users, passwords, Quick Connect
requests, PINs, devices — and records every request it received, so a test can assert
what was *not* sent as easily as what was.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast
from urllib.parse import parse_qs

import httpx2

#: Ids Jellyfin and Emby hand out: 32 hexadecimal digits.
ADMIN_ID: Final = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
USER_ID: Final = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
SERVER_ID: Final = "9f8e7d6c5b4a39281706f5e4d3c2b1a0"
ADMIN_API_KEY: Final = "admin-api-key"
MACHINE_ID: Final = "plexmachine00112233445566778899aa"


def media_browser_user(
    user_id: str = ADMIN_ID,
    name: str = "Alex",
    *,
    admin: bool = True,
    remote_access: bool = True,
    disabled: bool = False,
) -> dict[str, Any]:
    """A user as Jellyfin and Emby serialise one."""
    return {
        "Id": user_id,
        "Name": name,
        "Policy": {
            "IsAdministrator": admin,
            "EnableRemoteAccess": remote_access,
            "IsDisabled": disabled,
        },
    }


def _json(payload: object, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, json=payload)


def _xml(body: str, status: int = 200) -> httpx2.Response:
    return httpx2.Response(status, content=body.encode(), headers={"content-type": "text/xml"})


def _token_of(request: httpx2.Request) -> str | None:
    """The ``Token="…"`` of an ``Authorization: MediaBrowser …`` header, if there is one."""
    header = request.headers.get("authorization", "")
    for part in header.removeprefix("MediaBrowser ").split(","):
        name, _, value = part.strip().partition("=")
        if name == "Token":
            return value.strip('"')
    return None


def _body(request: httpx2.Request) -> Mapping[str, Any]:
    try:
        payload: Any = json.loads(request.content or b"{}")
    except ValueError:
        return {}
    return cast("Mapping[str, Any]", payload) if isinstance(payload, dict) else {}


@dataclass
class QuickConnectRequest:
    """One Quick Connect request, as Jellyfin keeps it."""

    code: str
    secret: str
    authenticated: bool = False
    user_name: str | None = None


@dataclass
class FakeMediaBrowser:
    """A Jellyfin or Emby server answering from memory."""

    kind: str = "jellyfin"
    server_id: str = SERVER_ID
    server_name: str = "Home Jellyfin"
    version: str = "10.10.3"
    product_name: str | None = None
    api_key: str = ADMIN_API_KEY
    users: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    passwords: dict[str, str] = field(default_factory=dict[str, str])
    quick_connect_enabled: bool = True
    #: Set to answer ``403`` to ``AuthenticateByName`` (disabled user, MaxActiveSessions).
    forbidden_users: set[str] = field(default_factory=set[str])
    #: When set, every call raises a connection error instead of answering.
    offline: bool = False
    #: ``path -> status`` forced on the next call to that path.
    fails: dict[str, int] = field(default_factory=dict[str, int])
    #: Quick Connect requests by secret, and the tokens ``/Sessions/Logout`` received.
    quick_connect: dict[str, QuickConnectRequest] = field(
        default_factory=dict[str, QuickConnectRequest]
    )
    logouts: list[str] = field(default_factory=list[str])
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])
    _tokens: dict[str, str] = field(default_factory=dict[str, str])
    _next_secret: int = 0

    # --- helpers for the tests ------------------------------------------------------

    def add_user(self, name: str, password: str, **fields: Any) -> dict[str, Any]:
        """Register a user with a password and return the payload the server serves."""
        user = media_browser_user(name=name, **fields)
        self.users[name] = user
        self.passwords[name] = password
        return user

    def start_quick_connect(self, code: str = "123456", secret: str = "qc-secret") -> str:  # noqa: S107
        """Pre-register a Quick Connect request, as ``Initiate`` would."""
        self.quick_connect[secret] = QuickConnectRequest(code=code, secret=secret)
        return secret

    def approve(self, secret: str, user_name: str) -> None:
        """Approve a Quick Connect request, as a signed-in Jellyfin client would."""
        request = self.quick_connect[secret]
        request.authenticated = True
        request.user_name = user_name

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the adapters talk through."""
        return httpx2.MockTransport(self.handle)

    @property
    def product(self) -> str:
        """``ProductName``: the real one unless the test overrode it."""
        if self.product_name is not None:
            return self.product_name
        return "Jellyfin Server" if self.kind == "jellyfin" else "Emby Server"

    # --- the server -----------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("no route to host")
        path = request.url.path
        forced = self.fails.pop(path, None)
        if forced is not None:
            return httpx2.Response(forced, json={"error": "forced"})
        route = self._routes().get((request.method, path))
        if route is None:
            return _json({"error": "not found"}, 404)
        return route(request)

    def _routes(self) -> dict[tuple[str, str], Callable[[httpx2.Request], httpx2.Response]]:
        return {
            ("GET", "/System/Info/Public"): self._public_info,
            ("GET", "/Users"): self._list_users,
            ("POST", "/Users/AuthenticateByName"): self._authenticate,
            ("POST", "/Sessions/Logout"): self._logout,
            ("GET", "/QuickConnect/Enabled"): self._quick_connect_enabled,
            ("POST", "/QuickConnect/Initiate"): self._quick_connect_initiate,
            ("GET", "/QuickConnect/Connect"): self._quick_connect_connect,
            ("POST", "/Users/AuthenticateWithQuickConnect"): self._quick_connect_authenticate,
        }

    def _public_info(self, _request: httpx2.Request) -> httpx2.Response:
        info: dict[str, Any] = {
            "Id": self.server_id,
            "ServerName": self.server_name,
            "Version": self.version,
            "StartupWizardCompleted": True,
        }
        if self.product:
            info["ProductName"] = self.product
        return _json(info)

    def _list_users(self, request: httpx2.Request) -> httpx2.Response:
        if _token_of(request) != self.api_key:
            return _json({"error": "unauthorized"}, 401)
        return _json(list(self.users.values()))

    def _authenticate(self, request: httpx2.Request) -> httpx2.Response:
        body = _body(request)
        name, password = body.get("Username"), body.get("Pw")
        if not isinstance(name, str) or self.passwords.get(name) != password:
            return _json({"error": "invalid"}, 401)
        if name in self.forbidden_users:
            return _json({"error": "forbidden"}, 403)
        return _json(self._session_for(name))

    def _session_for(self, name: str) -> dict[str, Any]:
        token = f"user-token-{name}"
        self._tokens[token] = name
        return {"User": self.users[name], "AccessToken": token, "ServerId": self.server_id}

    def _logout(self, request: httpx2.Request) -> httpx2.Response:
        token = _token_of(request)
        if token is None or token not in self._tokens:
            return _json({"error": "unauthorized"}, 401)
        self.logouts.append(token)
        return httpx2.Response(204)

    def _quick_connect_enabled(self, _request: httpx2.Request) -> httpx2.Response:
        if self.kind != "jellyfin":
            return _json({"error": "not found"}, 404)
        return _json(self.quick_connect_enabled)

    def _quick_connect_initiate(self, _request: httpx2.Request) -> httpx2.Response:
        if self.kind != "jellyfin" or not self.quick_connect_enabled:
            return _json({"error": "disabled"}, 401)
        self._next_secret += 1
        secret = f"qc-secret-{self._next_secret}"
        code = f"12345{self._next_secret}"
        self.quick_connect[secret] = QuickConnectRequest(code=code, secret=secret)
        return _json({"Secret": secret, "Code": code, "Authenticated": False})

    def _quick_connect_connect(self, request: httpx2.Request) -> httpx2.Response:
        secret = parse_qs(request.url.query.decode()).get("secret", [""])[0]
        pending = self.quick_connect.get(secret)
        if pending is None:
            return _json({"error": "unknown"}, 404)
        return _json({"Authenticated": pending.authenticated, "Code": pending.code})

    def _quick_connect_authenticate(self, request: httpx2.Request) -> httpx2.Response:
        secret = _body(request).get("Secret")
        pending = self.quick_connect.get(secret) if isinstance(secret, str) else None
        if pending is None or not pending.authenticated or pending.user_name is None:
            return _json({"error": "not authenticated"}, 401)
        if pending.user_name in self.forbidden_users:
            return _json({"error": "forbidden"}, 403)
        return _json(self._session_for(pending.user_name))


@dataclass
class FakePin:
    """A plex.tv PIN."""

    id: str
    code: str
    client_id: str
    token: str | None = None


@dataclass
class FakePlexTv:
    """plex.tv, with PINs, accounts, resources, devices and shared users."""

    #: ``token -> (account id, display name)``.
    accounts: dict[str, tuple[str, str]] = field(default_factory=dict[str, tuple[str, str]])
    #: ``token -> resources``, each a plex.tv resource object.
    resources: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    #: ``client identifier -> plex.tv device id``.
    devices: dict[str, str] = field(default_factory=dict[str, str])
    #: Accounts the server is shared with: ``(account id, name, machine identifier)``.
    shared: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    pins: dict[str, FakePin] = field(default_factory=dict[str, FakePin])
    pin_lifetime: timedelta = timedelta(minutes=15)
    #: Paths that answer ``429`` (``Retry-After: 2``) instead of their usual answer.
    rate_limited: set[str] = field(default_factory=set[str])
    #: Set to make ``DELETE /devices/{id}.xml`` fail, as the unofficial endpoint may.
    device_deletion_fails: bool = False
    offline: bool = False
    requests: list[httpx2.Request] = field(default_factory=list[httpx2.Request])
    _next_pin: int = 0

    # --- helpers for the tests ------------------------------------------------------

    def add_account(  # noqa: PLR0913
        self,
        token: str,
        account_id: str,
        name: str = "Robin",
        *,
        machine_id: str = MACHINE_ID,
        owned: bool = True,
        provides: str = "server",
    ) -> None:
        """Give a token an account and one resource for the configured server."""
        self.accounts[token] = (account_id, name)
        self.resources[token] = [
            {
                "name": "Home Plex",
                "clientIdentifier": machine_id,
                "provides": provides,
                "owned": owned,
            }
        ]

    def approve(self, code: str, token: str) -> None:
        """Approve the PIN with this code, as the plex.tv page would."""
        for pin in self.pins.values():
            if pin.code == code:
                pin.token = token
                # plex.tv device ids are numeric, as ``/devices.xml`` returns them.
                self.devices.setdefault(pin.client_id, str(1000 + len(self.devices) + 1))
                return
        raise KeyError(code)

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the plex.tv adapter talks through."""
        return httpx2.MockTransport(self.handle)

    # --- the service ----------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:  # noqa: PLR0911
        """Answer one request."""
        self.requests.append(request)
        if self.offline:
            raise httpx2.ConnectError("plex.tv is down")
        path = request.url.path
        if path in self.rate_limited:
            return _json({"error": "slow down"}, 429)
        if path == "/api/v2/pins" and request.method == "POST":
            return self._create_pin(request)
        if path.startswith("/api/v2/pins/"):
            return self._read_pin(request, path.removeprefix("/api/v2/pins/"))
        if path == "/api/v2/user":
            return self._account(request)
        if path == "/api/v2/resources":
            return self._resources(request)
        if path == "/devices.xml":
            return self._devices(request)
        if path.startswith("/devices/") and request.method == "DELETE":
            return self._delete_device(path)
        if path == "/api/users":
            return self._shared_users(request)
        return _json({"error": "not found"}, 404)

    def _create_pin(self, request: httpx2.Request) -> httpx2.Response:
        client_id = request.headers.get("x-plex-client-identifier", "")
        self._next_pin += 1
        pin = FakePin(
            id=str(1000 + self._next_pin), code=f"CODE{self._next_pin}", client_id=client_id
        )
        self.pins[pin.id] = pin
        expires_at = datetime.now(UTC) + self.pin_lifetime
        return _json(
            {
                "id": int(pin.id),
                "code": pin.code,
                "clientIdentifier": client_id,
                "authToken": None,
                "expiresAt": expires_at.isoformat().replace("+00:00", "Z"),
                "expiresIn": int(self.pin_lifetime.total_seconds()),
            },
            201,
        )

    def _read_pin(self, request: httpx2.Request, pin_id: str) -> httpx2.Response:
        pin = self.pins.get(pin_id)
        if pin is None or request.headers.get("x-plex-client-identifier") != pin.client_id:
            return _json({"error": "not found"}, 404)
        return _json({"id": int(pin.id), "code": pin.code, "authToken": pin.token})

    def _account(self, request: httpx2.Request) -> httpx2.Response:
        if not request.headers.get("x-plex-client-identifier"):
            # plex.tv refuses its v2 endpoints without one.
            return _json({"error": "X-Plex-Client-Identifier is missing"}, 400)
        account = self.accounts.get(request.headers.get("x-plex-token", ""))
        if account is None:
            return _json({"error": "unauthorized"}, 401)
        return _json({"id": int(account[0]), "uuid": "uuid", "title": account[1]})

    def _resources(self, request: httpx2.Request) -> httpx2.Response:
        if not request.headers.get("x-plex-client-identifier"):
            return _json({"error": "X-Plex-Client-Identifier is missing"}, 400)
        token = request.headers.get("x-plex-token", "")
        if token not in self.accounts:
            return _json({"error": "unauthorized"}, 401)
        return _json(self.resources.get(token, []))

    def _devices(self, request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("x-plex-token", "") not in self.accounts:
            return _xml("<MediaContainer />", 401)
        rows = "".join(
            f'<Device id="{device_id}" clientIdentifier="{client_id}" name="Tindeerr" />'
            for client_id, device_id in self.devices.items()
        )
        return _xml(f"<MediaContainer>{rows}</MediaContainer>")

    def _delete_device(self, path: str) -> httpx2.Response:
        if self.device_deletion_fails:
            return _xml("<MediaContainer />", 500)
        device_id = path.removeprefix("/devices/").removesuffix(".xml")
        for client_id, known in list(self.devices.items()):
            if known == device_id:
                del self.devices[client_id]
        return _xml("<MediaContainer />")

    def _shared_users(self, request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("x-plex-token", "") not in self.accounts:
            return _xml("<MediaContainer />", 401)
        rows = "".join(
            f'<User id="{account_id}" title="{name}">'
            f'<Server machineIdentifier="{machine_id}" /></User>'
            for account_id, name, machine_id in self.shared
        )
        return _xml(f"<MediaContainer>{rows}</MediaContainer>")


#: Where the tests place each server. The names never resolve: every call is mocked.
MEDIA_SERVER_URL: Final = "http://media.lan:8096"
PLEX_SERVER_URL: Final = "http://plex.lan:32400"
_PLEX_HOST: Final = "plex.lan"


@dataclass
class FakeInternet:
    """Everything outside Tindeerr: the media server, the Plex server and plex.tv.

    One transport routes by host, so the application can be wired with the **real**
    adapters and still touch nothing: a test changes the fakes and watches what the
    server does with them.
    """

    media: FakeMediaBrowser = field(default_factory=FakeMediaBrowser)
    plex_tv: FakePlexTv = field(default_factory=FakePlexTv)
    machine_id: str = MACHINE_ID
    plex_offline: bool = False

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Route a request to the server that answers at its host."""
        if request.url.host == _PLEX_HOST:
            return self._plex(request)
        return self.media.handle(request)

    def _plex(self, request: httpx2.Request) -> httpx2.Response:
        if self.plex_offline:
            raise httpx2.ConnectError("no route to host")
        if request.url.path == "/identity":
            return _json({"MediaContainer": {"machineIdentifier": self.machine_id}})
        if request.url.path == "/":
            known = request.headers.get("x-plex-token", "") in self.plex_tv.accounts
            return _json({"MediaContainer": {}}, 200 if known else 401)
        return _json({"error": "not found"}, 404)

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport the media server adapters talk through."""
        return httpx2.MockTransport(self.handle)
