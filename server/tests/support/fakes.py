"""A clock that only moves when told to, and a media server that answers from memory."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import (
    ConnectionCheck,
    ConnectorHealth,
    MediaServerConnection,
    MediaServerKind,
    MediaUser,
    QuickConnectStart,
    ServerIdentity,
)

#: The moment every test starts at, unless it says otherwise.
START = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


class FakeClock:
    """A clock under the test's control; ``sleep`` returns at once and moves time on."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self._monotonic = 10_000.0
        #: Every pause a limiter asked for, in seconds, in order.
        self.slept: list[float] = []

    def now(self) -> datetime:
        """Current time."""
        return self._now

    def monotonic(self) -> float:
        """Monotonic seconds, moving with ``advance`` and ``sleep``."""
        return self._monotonic

    async def sleep(self, seconds: float) -> None:
        """Record the pause and move time forward instead of waiting."""
        self.slept.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float | timedelta) -> None:
        """Move both clocks forward."""
        amount = seconds.total_seconds() if isinstance(seconds, timedelta) else seconds
        self._now += timedelta(seconds=amount)
        self._monotonic += amount


def media_user(
    user_id: str = "0" * 32,
    name: str = "Alex",
    *,
    admin: bool = True,
    remote_access: bool = True,
    disabled: bool = False,
) -> MediaUser:
    """Build a ``MediaUser`` as an adapter would return it."""
    return MediaUser(
        id=user_id, name=name, is_admin=admin, remote_access=remote_access, disabled=disabled
    )


@dataclass
class FakeMediaServer:
    """A media server adapter that answers from memory and records what it was asked."""

    kind: MediaServerKind = "jellyfin"
    health: ConnectorHealth = "ok"
    server_id: str = "server-1"
    server_name: str = "Home Jellyfin"
    server_version: str = "10.10.3"
    #: Users by name, and their passwords; sign-in raises for anything else.
    users: dict[str, MediaUser] = field(default_factory=dict[str, MediaUser])
    passwords: dict[str, str] = field(default_factory=dict[str, str])
    quick_connect_code: str = "123456"
    quick_connect_user: MediaUser | None = None
    quick_connect_on: bool = True
    #: What ``identify`` claims to be; defaults to ``kind``.
    identifies_as: MediaServerKind | None = None
    calls: list[str] = field(default_factory=list[str])

    async def identify(self) -> ServerIdentity:
        """Return the configured identity."""
        self.calls.append("identify")
        return ServerIdentity(
            kind=self.identifies_as or self.kind,
            server_id=self.server_id,
            name=self.server_name,
            version=self.server_version,
        )

    async def test(self) -> ConnectionCheck:
        """Return the configured health."""
        self.calls.append("test")
        return ConnectionCheck(self.health, self.server_name, self.server_version)

    async def authenticate_password(self, username: str, password: str) -> MediaUser:
        """Check a password against ``passwords`` and return the matching user."""
        self.calls.append("authenticate_password")
        user = self.users.get(username)
        if user is None or self.passwords.get(username) != password:
            raise ProblemError(401, "invalid_credentials", "Wrong user name or password.")
        if user.disabled:
            raise ProblemError(403, "account_disabled", "This account is disabled.")
        return user

    async def quick_connect_enabled(self) -> bool:
        """Say whether Quick Connect is on, without calling anything."""
        self.calls.append("quick_connect_enabled")
        return self.kind == "jellyfin" and self.quick_connect_on

    async def quick_connect_start(self) -> QuickConnectStart:
        """Start a Quick Connect request."""
        self.calls.append("quick_connect_start")
        return QuickConnectStart(self.quick_connect_code, "secret-1")

    async def quick_connect_poll(self, secret: str) -> MediaUser | None:
        """Return the approving user, if the test set one."""
        self.calls.append(f"quick_connect_poll:{secret}")
        return self.quick_connect_user

    async def list_users(self) -> list[MediaUser]:
        """Return every known user."""
        self.calls.append("list_users")
        return list(self.users.values())


class FakeMediaServers:
    """A ``MediaServerFactory`` returning one fake and recording the connections asked for."""

    def __init__(self, server: FakeMediaServer | None = None) -> None:
        self.server = server or FakeMediaServer()
        self.connections: list[MediaServerConnection] = []

    def __call__(self, connection: MediaServerConnection) -> FakeMediaServer:
        """Return the fake, after recording the connection it was asked for."""
        self.connections.append(connection)
        return self.server

    @property
    def last(self) -> MediaServerConnection:
        """The connection of the most recent call."""
        return self.connections[-1]
