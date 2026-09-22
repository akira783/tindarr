"""Media server port: what auth and the swipe engine ask a Jellyfin, Emby or Plex server.

Adapters (step 3 of the roadmap for the library parts, step 2b for the sign-in parts)
implement ``MediaServer``; ``tindarr.main`` builds one from the stored connector
settings through a ``MediaServerFactory``. The wire details (the
``Authorization: MediaBrowser …`` header, ``X-Plex-Token``) belong to the adapters; only
values cross this boundary.

Adapters report failures by raising ``tindarr.core.errors.ProblemError`` with the
contract's codes (``media_server_unreachable``, ``invalid_credentials``,
``account_disabled``, ``plex_tv_unreachable``…), except for ``test``, which returns a
coarse health value and never raises for a remote failure.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, get_args

type MediaServerKind = Literal["jellyfin", "emby", "plex"]
type ConnectorHealth = Literal[
    "ok", "unauthorized", "unreachable", "unexpected_response", "unsupported_version"
]

MEDIA_SERVER_KINDS: Final[tuple[MediaServerKind, ...]] = get_args(MediaServerKind.__value__)
#: Jellyfin and Emby user ids are 32 hex digits; Plex users are keyed by account id.
_JELLYFIN_ID_LENGTH: Final = 32
#: Longest server id kept from ``/System/Info/Public``; real ones are 32 hex digits.
_MAX_SERVER_ID_LENGTH: Final = 128


def as_media_server_kind(value: object) -> MediaServerKind | None:
    """Return ``value`` as a media server kind, or ``None`` if it is not one."""
    return next((kind for kind in MEDIA_SERVER_KINDS if kind == value), None)


def normalize_user_id(kind: MediaServerKind, raw: str) -> str:
    """Return the stored form of a media server user id (docs/auth.md, section 4).

    Jellyfin and Emby: 32 lower-case hex digits, dashes removed. Plex: the plex.tv
    account id, a decimal string. Raises ``ValueError`` for anything else, so a server
    answering something unexpected never silently links to another user's row.
    """
    value = raw.strip()
    if kind == "plex":
        if not value.isdigit():
            msg = "a Plex user id must be the decimal plex.tv account id"
            raise ValueError(msg)
        return value.lstrip("0") or "0"
    value = value.replace("-", "").lower()
    if len(value) != _JELLYFIN_ID_LENGTH or not all(c in "0123456789abcdef" for c in value):
        msg = f"a {kind} user id must be 32 hexadecimal digits"
        raise ValueError(msg)
    return value


def normalize_server_id(raw: str) -> str | None:
    """Return the stored form of a media server's own id, or ``None`` when unusable.

    Lower case without dashes, like a user id, but deliberately not required to be 32
    hexadecimal digits: the identity is only ever compared with itself, so accepting a
    fork that numbers its servers differently costs nothing and refusing it would make
    the connector unsavable.
    """
    value = raw.strip().replace("-", "").lower()
    if not value or len(value) > _MAX_SERVER_ID_LENGTH:
        return None
    return value


@dataclass(frozen=True, slots=True)
class MediaUser:
    """A user as the media server describes them."""

    id: str
    name: str
    is_admin: bool
    remote_access: bool = True
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Who answered at the configured address."""

    kind: MediaServerKind
    server_id: str
    name: str | None = None
    version: str | None = None

    @property
    def key(self) -> str:
        """``<kind>:<server id>``, as stored in ``server_state.media_server_identity``."""
        return f"{self.kind}:{self.server_id}"

    @staticmethod
    def server_id_of(key: str | None, kind: MediaServerKind) -> str | None:
        """Return the server id inside a stored identity key, if it is one of ``kind``.

        The Plex sign-in reads the ``machineIdentifier`` this way: a key of another kind
        (the connector was repointed) must not match a resource.
        """
        if key is None:
            return None
        prefix, separator, server_id = key.partition(":")
        if not separator or prefix != kind or not server_id:
            return None
        return server_id


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """Result of a connection test: coarse health, never a response body."""

    health: ConnectorHealth
    server_name: str | None = None
    server_version: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the connector works."""
        return self.health == "ok"


@dataclass(frozen=True, slots=True)
class QuickConnectStart:
    """A Quick Connect request: the code the user approves, and the secret to poll with."""

    code: str
    secret: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MediaServerConnection:
    """Everything an adapter needs to talk to the configured server."""

    kind: MediaServerKind
    url: str
    secret: str
    verify_tls: bool = True
    #: Stable per install; the ``DeviceId`` of the Jellyfin / Emby client identity.
    device_id: str = ""


class MediaServer(Protocol):
    """What auth needs from a media server (the library parts arrive in step 3)."""

    kind: MediaServerKind

    async def identify(self) -> ServerIdentity:
        """Read the server's own identity (id, product, version)."""
        ...

    async def test(self) -> ConnectionCheck:
        """Check the address and the credential, without raising for a remote failure."""
        ...

    async def authenticate_password(self, username: str, password: str) -> MediaUser:
        """Check a user's password (Jellyfin and Emby) and return the user."""
        ...

    async def quick_connect_enabled(self) -> bool:
        """Whether Quick Connect is switched on here (Jellyfin only; cached by auth)."""
        ...

    async def quick_connect_start(self) -> QuickConnectStart:
        """Start a Quick Connect request (Jellyfin)."""
        ...

    async def quick_connect_poll(self, secret: str) -> MediaUser | None:
        """Return the user once the Quick Connect request was approved, else ``None``."""
        ...

    async def list_users(self) -> list[MediaUser]:
        """Every user of the server, for the periodic sync."""
        ...


class MediaServerFactory(Protocol):
    """Builds the adapter for a connection. Only ``tindarr.main`` implements it."""

    def __call__(self, connection: MediaServerConnection) -> MediaServer:
        """Return an adapter talking to ``connection``."""
        ...
