"""The request backend port: Seerr, and its Jellyseerr and Overseerr relatives.

Everything here follows one rule from the security model (section 2): **a request is
filed as the user who asked for it**, never as the admin key that carries it. The admin
key is what lets Tindarr talk to the backend at all; ``X-API-User`` is what makes the
backend apply that user's permissions, quotas, auto-approval and override rules. A
request body's own ``userId`` field would not: the backend would still weigh
auto-approval against the API key's administrator and wave everything through.

Users are therefore matched before anything is requested. The backend's user list is
read with the admin key **without** ``X-API-User`` (asking on behalf of a user returns
that user alone), and a Tindarr user is matched to it by the media server id the backend
stores — ``jellyfinUserId`` for Jellyfin and Emby, ``plexId`` for Plex — normalised on
both sides, never by name or email.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.media_server import MediaUser
from tindarr.ports.titles import TitleRef

#: How far a title has got, named as the HTTP contract's ``Availability``.
type Availability = Literal["none", "requested", "processing", "partially_available", "available"]
#: What filing a request produced, named as the contract's ``RequestStatus``.
type RequestStatus = Literal[
    "queued", "awaiting_approval", "already_requested", "already_available"
]
#: Which seasons of a series a request asks for (the administrator's choice).
type SeasonPolicy = Literal["all", "first"]


@dataclass(frozen=True, slots=True)
class BackendUser:
    """A user of the request backend, matched to a media server account."""

    id: int
    display_name: str
    #: The normalised media server id the backend holds for them, when it holds one.
    media_server_user_id: str | None = None
    #: Whether the backend says this user may request anything at all.
    can_request: bool = True


@dataclass(frozen=True, slots=True)
class RequestResult:
    """What the backend did with a request."""

    status: RequestStatus
    request_id: int | None = None
    availability: Availability = "requested"


class RequestBackend(Protocol):
    """Seerr v3 and its relatives, behind one interface."""

    async def test(self) -> ConnectionCheck:
        """Check the address and the API key, without raising for a remote failure."""
        ...

    async def find_user(self, media_user: MediaUser) -> BackendUser | None:
        """Return the backend user that is this media server account, if there is one."""
        ...

    async def request(self, title: TitleRef, on_behalf_of: BackendUser) -> RequestResult:
        """File a request as ``on_behalf_of``, with their own permissions and quotas."""
        ...

    async def status(self, titles: Sequence[TitleRef]) -> dict[TitleRef, Availability]:
        """Return how far each title has got; unknown titles come back as ``none``."""
        ...
