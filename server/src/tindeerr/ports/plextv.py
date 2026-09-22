"""plex.tv port: the account service behind every Plex sign-in (docs/auth.md, §4).

Plex has no password endpoint a third party may use: Tindeerr brokers a **PIN**. It
creates one on plex.tv, the user approves it in their browser, and the PIN then carries
a token. Tindeerr uses that token once — to read the account and the servers it may
reach — then deletes the plex.tv device it created, so no live "Tindeerr" device stays
in the user's account. The only Plex token Tindeerr stores is the owner token of the
media server connector.

Two rules of this port matter for security and are checked by its callers, never by the
adapter:

- a resource is the configured server only when its ``client_identifier`` equals the
  stored ``machineIdentifier``; names and advertised URLs are self-reported;
- the account administers it only when ``owned`` is true **on that resource**.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from tindeerr.ports.media_server import MediaUser, normalize_user_id
from tindeerr.ports.problems import plex_tv_unreachable

#: What plex.tv resources call a media server in ``provides``.
SERVER_PROVIDES: Final = "server"


@dataclass(frozen=True, slots=True)
class PlexPin:
    """A plex.tv PIN, with the client identifier it was created for.

    ``client_id`` is part of the PIN's identity: polling it with another identifier
    fails, and the device the approval creates belongs to that identifier. Sign-in PINs
    use a fresh one, so deleting their device never touches the install's own.
    """

    id: str
    code: str
    client_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PlexAccount:
    """A plex.tv account: the decimal id users are keyed by, and a display name."""

    id: str
    name: str


@dataclass(frozen=True, slots=True)
class PlexResource:
    """A server (or player) a plex.tv account may reach."""

    client_identifier: str
    name: str
    owned: bool
    provides: tuple[str, ...] = ()

    @property
    def is_server(self) -> bool:
        """Whether this resource is a media server rather than a player."""
        return SERVER_PROVIDES in self.provides


def as_media_user(account: PlexAccount, *, admin: bool) -> MediaUser:
    """Return a plex.tv account as a media server user (docs/auth.md, section 4).

    Plex users are keyed by their plex.tv account id, never by name or email, and Plex
    has no remote-access policy, so ``remote_access`` is always true.
    """
    try:
        user_id = normalize_user_id("plex", account.id)
    except ValueError:
        raise plex_tv_unreachable() from None
    return MediaUser(id=user_id, name=account.name, is_admin=admin, remote_access=True)


class PlexTv(Protocol):
    """What auth and the Plex adapter ask plex.tv.

    Failures are ``ProblemError`` with the contract's codes: ``plex_tv_unreachable``
    when plex.tv does not answer usably, ``rate_limited`` when it asks to slow down.
    """

    async def create_pin(self, client_id: str, device_name: str) -> PlexPin:
        """Create a strong PIN for ``client_id``; ``device_name`` names it on plex.tv."""
        ...

    async def check_pin(self, pin: PlexPin) -> str | None:
        """Return the PIN's token once it was approved, else ``None``."""
        ...

    async def account(self, token: str) -> PlexAccount:
        """Return the account a token belongs to."""
        ...

    async def resources(self, token: str) -> list[PlexResource]:
        """Return the resources this token may reach."""
        ...

    async def delete_device(self, token: str, client_id: str) -> bool:
        """Delete the plex.tv device of ``client_id``; ``False`` when it could not be done.

        Unofficial (``/devices.xml``), so it never raises: a sign-in that worked is not
        undone because the cleanup failed.
        """
        ...

    async def shared_users(self, owner_token: str, machine_id: str) -> list[PlexAccount]:
        """Return the accounts this server is shared with (the hourly sync)."""
        ...
