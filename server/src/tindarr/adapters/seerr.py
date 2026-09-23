"""Seerr, Jellyseerr and Overseerr behind the request backend port (roadmap step 3).

The three products share one API, so they share one adapter. What matters here is not
the endpoints but **whose** request it is, and the answer is the same rule the security
model gives (section 2): the admin API key is what lets Tindarr talk to the backend at
all, and ``X-API-User`` is what makes the backend treat the request as that person's.
Both headers travel together on a request, and only the key travels on the user listing.

Why the request body's own ``userId`` field is not used, though it looks equivalent: the
backend attributes the request to that user but still weighs **auto-approval** against
the API key's administrator. Every request would then be approved without review, and a
household's request quotas would mean nothing. ``X-API-User`` applies the user's own
permissions, quotas, auto-approval and override rules, which is the whole point.

Why the user list is read **without** ``X-API-User``: asking on behalf of somebody
returns that somebody, so the listing would only ever contain the user Tindarr already
knew. Users are matched on the media server id the backend stores — ``jellyfinUserId``
for Jellyfin and Emby, ``plexId`` for Plex — normalised on both sides with the port's
own rule, never by name or email, which change and collide.

Overseerr has no Jellyfin user ids at all, so it is the Plex-only member of the family
([ADR 0005](../../../../docs/adr/0005-adapters-and-ai-providers.md)).
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any, Final

import httpx2

from tindarr.adapters.http import (
    NO_RESPONSE_REASONS,
    HttpSession,
    RemoteCallError,
    as_object,
    as_object_list,
    as_text,
    read_mapping,
)
from tindarr.ports import problems
from tindarr.ports.connectors import ConnectionCheck, ConnectorHealth
from tindarr.ports.media_server import MediaServerKind, MediaUser, normalize_user_id
from tindarr.ports.request_backend import (
    Availability,
    BackendUser,
    RequestResult,
    RequestStatus,
    SeasonPolicy,
)
from tindarr.ports.titles import TitleRef

#: Every endpoint lives under this; the configured URL is the server's root.
API_PREFIX: Final = "/api/v1"
#: The header that carries the admin key.
API_KEY_HEADER: Final = "X-Api-Key"
#: The header that makes a request somebody else's. Seerr matches it case-insensitively,
#: as every HTTP header; this is the spelling its own source uses.
ON_BEHALF_HEADER: Final = "X-API-User"
#: Users read per page. The backend caps ``take`` well above this.
USER_PAGE_SIZE: Final = 100
#: Pages read before giving up on a backend whose ``skip`` does nothing.
MAX_USER_PAGES: Final = 50
#: How many availability lookups run at once. One per card is otherwise a slow deck.
STATUS_CONCURRENCY: Final = 5
#: Seerr's ``MediaStatus`` enum, mapped to the contract's ``Availability``.
MEDIA_STATUS: Final[Mapping[int, Availability]] = {
    1: "none",  # UNKNOWN
    2: "requested",  # PENDING
    3: "processing",  # PROCESSING
    4: "partially_available",  # PARTIALLY_AVAILABLE
    5: "available",  # AVAILABLE
}
#: Seerr's ``MediaRequestStatus``: 1 is waiting for an administrator, 2 is approved.
_REQUEST_PENDING: Final = 1
#: Words that tell a spent quota from a missing permission in a ``403``. The message is
#: only ever *read*: nothing the backend wrote is passed on to the caller.
_QUOTA_WORDS: Final = ("quota", "limit")

logger = logging.getLogger(__name__)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def media_server_user_id(row: Mapping[str, Any], kind: MediaServerKind) -> str | None:
    """Read the media server id the backend holds for one of its users.

    Normalised with the port's own rule, so ``8A1B…`` on one side and ``8a1b-2c3d-…`` on
    the other are the same person, and anything that is not an id of that shape matches
    nobody rather than matching the first row.
    """
    raw = as_text(row.get("plexId")) if kind == "plex" else as_text(row.get("jellyfinUserId"))
    if raw is None:
        return None
    try:
        return normalize_user_id(kind, raw)
    except ValueError:
        return None


def backend_user(row: Mapping[str, Any], kind: MediaServerKind) -> BackendUser | None:
    """Build a ``BackendUser`` from one row of the backend's user list."""
    user_id = _int(row.get("id"))
    if user_id is None:
        return None
    name = (
        as_text(row.get("displayName"))
        or as_text(row.get("jellyfinUsername"))
        or as_text(row.get("plexUsername"))
        or as_text(row.get("username"))
        or str(user_id)
    )
    return BackendUser(
        id=user_id,
        display_name=name,
        media_server_user_id=media_server_user_id(row, kind),
    )


class SeerrBackend:
    """The ``RequestBackend`` port over the Seerr family."""

    def __init__(  # noqa: PLR0913 - one argument per stored connector field
        self,
        url: str,
        api_key: str,
        media_server_kind: MediaServerKind,
        *,
        tv_seasons: SeasonPolicy = "all",
        verify_tls: bool = True,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._kind: MediaServerKind = media_server_kind
        self._tv_seasons: SeasonPolicy = tv_seasons
        self._verify_tls = verify_tls
        self._transport = transport

    # --- plumbing -------------------------------------------------------------------

    def _session(self) -> HttpSession:
        return HttpSession(
            self._url,
            headers={"Accept": "application/json", API_KEY_HEADER: self._api_key},
            verify_tls=self._verify_tls,
            transport=self._transport,
        )

    @staticmethod
    def _expect_ok(response: httpx2.Response) -> httpx2.Response:
        if response.status_code != HTTPStatus.OK:
            raise RemoteCallError(f"status_{response.status_code}")
        return response

    @staticmethod
    def _unreachable(operation: str, failure: RemoteCallError) -> Exception:
        logger.warning(
            "the request backend did not answer usably",
            extra={"operation": operation, "reason": failure.reason},
        )
        return problems.request_backend_error()

    @staticmethod
    def _health_of(failure: RemoteCallError) -> ConnectorHealth:
        return "unreachable" if failure.reason in NO_RESPONSE_REASONS else "unexpected_response"

    # --- the connection test --------------------------------------------------------

    async def test(self) -> ConnectionCheck:
        """Check the address, then the key, the way the media server connector does.

        ``/status`` is public on every member of the family, so it proves the address
        and gives the version; the user list is what proves the key. A backend that
        answers the first and refuses the second is ``unauthorized``, which is the
        difference an administrator needs to see.
        """
        try:
            async with self._session() as session:
                response = await session.request_bounded("GET", f"{API_PREFIX}/status")
                payload = read_mapping(self._expect_ok(response))
                version = as_text(payload.get("version"))
                users = await session.request_bounded(
                    "GET", f"{API_PREFIX}/user", params={"take": "1", "skip": "0"}
                )
        except RemoteCallError as failure:
            return ConnectionCheck(self._health_of(failure))
        if users.status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return ConnectionCheck("unauthorized", None, version)
        if users.status_code != HTTPStatus.OK:
            return ConnectionCheck("unexpected_response", None, version)
        try:
            read_mapping(users)
        except RemoteCallError:
            return ConnectionCheck("unexpected_response", None, version)
        return ConnectionCheck("ok", None, version)

    # --- matching a user ------------------------------------------------------------

    async def find_user(self, media_user: MediaUser) -> BackendUser | None:
        """Return the backend user that is this media server account, or ``None``.

        The whole list is read — with the admin key and **no** ``X-API-User`` — and
        matched locally, because the backend offers no way to ask "who has this media
        server id?".
        """
        try:
            async with self._session() as session:
                for page in range(MAX_USER_PAGES):
                    rows = await self._user_page(session, page)
                    for row in rows:
                        found = backend_user(row, self._kind)
                        if found is not None and found.media_server_user_id == media_user.id:
                            return found
                    if len(rows) < USER_PAGE_SIZE:
                        return None
        except RemoteCallError as failure:
            raise self._unreachable("find_user", failure) from None
        logger.warning("stopped reading the request backend's users at the page cap")
        return None

    async def _user_page(self, session: HttpSession, page: int) -> list[Mapping[str, Any]]:
        response = await session.request_bounded(
            "GET",
            f"{API_PREFIX}/user",
            params={"take": str(USER_PAGE_SIZE), "skip": str(page * USER_PAGE_SIZE)},
        )
        payload = read_mapping(self._expect_ok(response))
        return as_object_list(payload.get("results"))

    # --- filing a request -----------------------------------------------------------

    def request_body(self, title: TitleRef) -> dict[str, Any]:
        """Build the request body: what, which seasons, and never in 4K.

        ``is4k`` is sent explicitly as false rather than left out. A 4K request needs a
        separate permission on the backend and pulls a different, much larger file; a
        household that wants it says so there, not through a field Tindarr omitted.
        """
        body: dict[str, Any] = {
            "mediaType": title.kind,
            "mediaId": title.tmdb_id,
            "is4k": False,
        }
        if title.kind == "tv":
            # "all" is the string the backend expects, not a list of every season.
            body["seasons"] = "all" if self._tv_seasons == "all" else [1]
        return body

    async def request(self, title: TitleRef, on_behalf_of: BackendUser) -> RequestResult:
        """File a request as ``on_behalf_of``, with their permissions and their quota."""
        try:
            async with self._session() as session:
                response = await session.request_bounded(
                    "POST",
                    f"{API_PREFIX}/request",
                    json_body=self.request_body(title),
                    headers={ON_BEHALF_HEADER: str(on_behalf_of.id)},
                )
        except RemoteCallError as failure:
            raise self._unreachable("request", failure) from None
        return self._result_of(response)

    def _result_of(self, response: httpx2.Response) -> RequestResult:
        status = response.status_code
        if status == HTTPStatus.CONFLICT:
            # The backend already holds a request for this title, from anyone.
            return RequestResult(status="already_requested")
        if status == HTTPStatus.FORBIDDEN:
            raise self._refusal(response)
        if status not in (HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.ACCEPTED):
            raise self._unreachable("request", RemoteCallError(f"status_{status}"))
        try:
            payload = read_mapping(response)
        except RemoteCallError as failure:
            raise self._unreachable("request", failure) from None
        return self._accepted(payload)

    @staticmethod
    def _accepted(payload: Mapping[str, Any]) -> RequestResult:
        request_id = _int(payload.get("id"))
        media = as_object(payload.get("media")) or {}
        media_status = MEDIA_STATUS.get(_int(media.get("status")) or 0, "requested")
        if media_status == "available":
            return RequestResult(
                status="already_available", request_id=request_id, availability="available"
            )
        waiting = _int(payload.get("status")) == _REQUEST_PENDING
        kind: RequestStatus = "awaiting_approval" if waiting else "queued"
        return RequestResult(status=kind, request_id=request_id, availability="requested")

    def _refusal(self, response: httpx2.Response) -> Exception:
        """Tell a spent quota from a missing permission, without repeating the message."""
        message = ""
        try:
            message = (as_text(read_mapping(response).get("message")) or "").casefold()
        except RemoteCallError:
            message = ""
        if any(word in message for word in _QUOTA_WORDS):
            return problems.quota_exceeded()
        return problems.request_not_allowed()

    # --- availability ---------------------------------------------------------------

    async def status(self, titles: Sequence[TitleRef]) -> dict[TitleRef, Availability]:
        """Return how far each title has got, ``none`` for anything unknown.

        One call per title, a few at a time. A title the backend fails on comes back as
        ``none`` rather than failing the whole deck: a missing badge is a missing badge,
        and the deck is what the user asked for.
        """
        if not titles:
            return {}
        wanted = list(dict.fromkeys(titles))
        gate = asyncio.Semaphore(STATUS_CONCURRENCY)
        async with self._session() as session:

            async def one(title: TitleRef) -> tuple[TitleRef, Availability]:
                async with gate:
                    return title, await self._availability(session, title)

            found = await asyncio.gather(*(one(title) for title in wanted))
        return dict(found)

    async def _availability(self, session: HttpSession, title: TitleRef) -> Availability:
        try:
            response = await session.request_bounded(
                "GET", f"{API_PREFIX}/{title.kind}/{title.tmdb_id}"
            )
            payload = read_mapping(self._expect_ok(response))
        except RemoteCallError as failure:
            logger.info(
                "the request backend did not answer for a title; it shows as not requested",
                extra={"reason": failure.reason},
            )
            return "none"
        media = as_object(payload.get("mediaInfo"))
        if media is None:
            return "none"
        return MEDIA_STATUS.get(_int(media.get("status")) or 0, "none")
