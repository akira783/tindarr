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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol, get_args

from tindarr.ports.connectors import ConnectionCheck, ConnectorHealth
from tindarr.ports.titles import MediaKind, TitleRef

__all__ = [
    "MEDIA_SERVER_KINDS",
    "NEGATIVE_ENGAGEMENT",
    "POSITIVE_ENGAGEMENT",
    "ConnectionCheck",
    "ConnectorHealth",
    "Engagement",
    "EngagementState",
    "LibraryIndex",
    "LibraryItem",
    "MediaKind",
    "MediaServer",
    "MediaServerConnection",
    "MediaServerFactory",
    "MediaServerKind",
    "MediaUser",
    "QuickConnectStart",
    "ServerIdentity",
    "as_media_server_kind",
    "film_engagement",
    "normalize_server_id",
    "normalize_user_id",
    "series_engagement",
]

type MediaServerKind = Literal["jellyfin", "emby", "plex"]

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


@dataclass(frozen=True, slots=True)
class LibraryItem:
    """One film or series the household already owns, as the media server lists it.

    ``tmdb_id`` is ``None`` for an item the media server never matched to TMDb (a home
    video, a badly named folder). Such an item cannot be compared with a suggestion, but
    it is still kept: it is what ``deep_link`` needs and what the console counts.
    """

    kind: MediaKind
    #: The media server's own id for the item; a Plex ``ratingKey``.
    item_id: str
    name: str
    tmdb_id: int | None = None
    year: int | None = None

    @property
    def ref(self) -> TitleRef | None:
        """The title this item is, when the media server matched it to TMDb."""
        return None if self.tmdb_id is None else TitleRef(self.kind, self.tmdb_id)


class LibraryIndex:
    """What the household already owns, ready to be looked up by TMDb id.

    The swipe engine asks two questions of it — "do we have this?" and "where do I send
    the user to watch it?" — so both are answered from one read of the library, through
    a map built once rather than a scan per suggestion.
    """

    def __init__(self, items: Iterable[LibraryItem] = ()) -> None:
        self.items: tuple[LibraryItem, ...] = tuple(items)
        # First wins: a duplicate is the same title in two libraries, and the first is
        # as good a link target as the second.
        by_ref: dict[TitleRef, LibraryItem] = {}
        for item in self.items:
            ref = item.ref
            if ref is not None:
                by_ref.setdefault(ref, item)
        self._by_ref: Mapping[TitleRef, LibraryItem] = by_ref

    def __len__(self) -> int:
        """How many items the library holds, matched to TMDb or not."""
        return len(self.items)

    @property
    def refs(self) -> frozenset[TitleRef]:
        """Every title the library holds and the media server matched to TMDb."""
        return frozenset(self._by_ref)

    def find(self, ref: TitleRef) -> LibraryItem | None:
        """Return the owned item for ``ref``, or ``None`` when it is not owned."""
        return self._by_ref.get(ref)

    def owns(self, ref: TitleRef) -> bool:
        """Whether the household already owns this title."""
        return ref in self._by_ref


#: Five states, ordered from "liked it enough to finish" to "gave up on it". They are
#: the fork's seven labels minus the two it never produces: ``rewatched``, which needs a
#: source that counts real completed views, and the movie/series spelling of
#: "finished" (``completed``), which says nothing extra once ``progress`` is carried.
type EngagementState = Literal["watched", "mostly_watched", "in_progress", "paused", "abandoned"]

#: States that say "this person likes this sort of thing".
POSITIVE_ENGAGEMENT: Final[frozenset[EngagementState]] = frozenset(
    {"watched", "mostly_watched", "in_progress"}
)
#: The one state that says the opposite. ``paused`` deliberately says neither: a title
#: somebody stopped watching months ago is as often a lost evening as a rejection.
NEGATIVE_ENGAGEMENT: Final[frozenset[EngagementState]] = frozenset({"abandoned"})


@dataclass(frozen=True, slots=True)
class Engagement:
    """What one user did with one title they own.

    Three numbers and nothing about how *often* it was played. Media server play counts
    are useless as a rewatch signal here: a debrid or shared setup inflates them (the
    fork measured nine "plays" for fifty-four minutes actually watched), so a title
    somebody half-watched would outrank one they loved. ``progress`` is the fraction
    watched — of the runtime for a film, of the episodes for a series — and that,
    with how long ago it happened, is all the engine reads.
    """

    item: LibraryItem
    state: EngagementState
    progress: float = 0.0
    episodes_played: int | None = None
    episodes_total: int | None = None
    last_played_at: datetime | None = None

    @property
    def ref(self) -> TitleRef | None:
        """The title this engagement is about, when it is one TMDb knows."""
        return self.item.ref

    @property
    def positive(self) -> bool:
        """Whether this is evidence in favour of more of the same."""
        return self.state in POSITIVE_ENGAGEMENT


#: A film counted as watched from here, even without the server's "played" flag: the
#: last tenth is credits, a phone call, or falling asleep two minutes from the end.
FINISHED_FILM_RATIO: Final = 0.9
#: A series counted as watched from here (the architecture's 60 % rule).
MOSTLY_WATCHED_RATIO: Final = 0.6
#: Watched this recently, it is being watched now, whatever the numbers say.
RECENT_DAYS: Final = 30
#: Untouched for this long, it is a candidate for "gave up on it".
ABANDONED_AFTER_DAYS: Final = 60
#: ...but only when little was invested: a third of a hundred-episode show, paused for
#: months, is a fan taking a break, not a rejection.
ABANDONED_RATIO: Final = 0.5
ABANDONED_MAX_EPISODES: Final = 5


def film_engagement(*, played: bool, progress: float, days_since: int | None) -> EngagementState:
    """Classify a film from the three facts every media server agrees on.

    The server's own "played" flag wins, then the position in the file, then how long
    ago it was touched. A film nobody has opened since before ``RECENT_DAYS`` and never
    finished was abandoned; one with no date at all is only ``paused``, because "we do
    not know when" is not evidence of anything.
    """
    if played or progress >= FINISHED_FILM_RATIO:
        return "watched"
    if days_since is not None and days_since <= RECENT_DAYS:
        return "in_progress"
    return "abandoned" if days_since is not None else "paused"


def series_engagement(
    *, episodes_played: int, episodes_total: int | None, days_since: int | None
) -> EngagementState:
    """Classify a series from how many of its episodes were played.

    ``MOSTLY_WATCHED_RATIO`` of them is "watched" and beats every other rule, recency
    included: four episodes of five is nearly finished, not dropped, however long ago it
    was. An unknown total can never reach that bar, so a series whose episode count the
    server did not give is judged on recency alone and never on a ratio.
    """
    ratio = episodes_played / episodes_total if episodes_total else None
    if ratio is not None and ratio >= MOSTLY_WATCHED_RATIO:
        return "watched" if ratio >= FINISHED_FILM_RATIO else "mostly_watched"
    if days_since is not None and days_since <= RECENT_DAYS:
        return "in_progress"
    barely_started = episodes_played <= ABANDONED_MAX_EPISODES and (
        ratio is None or ratio < ABANDONED_RATIO
    )
    if days_since is not None and days_since > ABANDONED_AFTER_DAYS and barely_started:
        return "abandoned"
    return "paused"


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

    async def library_ids(self) -> LibraryIndex:
        """Every film and series the household owns, with the TMDb ids matched to them."""
        ...

    async def engagement(self, user: MediaUser) -> list[Engagement]:
        """Return what this user has watched, is watching, or gave up on."""
        ...

    def deep_link(self, item: LibraryItem) -> str | None:
        """Return a URL that opens the item in this server's own client, if one exists."""
        ...


class MediaServerFactory(Protocol):
    """Builds the adapter for a connection. Only ``tindarr.main`` implements it."""

    def __call__(self, connection: MediaServerConnection) -> MediaServer:
        """Return an adapter talking to ``connection``."""
        ...
