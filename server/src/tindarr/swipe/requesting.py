"""Filing a request, as the person who asked for it and only for a title they were shown.

Two rules from [the security model](../../../../docs/security.md), and they are the
whole module:

**A request is filed as the caller's own backend user**, matched by the media server id
the backend stores, never by name and never as the admin key that carries the call. The
key is what lets Tindarr talk to Seerr at all; ``X-API-User`` is what makes Seerr apply
*that user's* permissions, quotas, auto-approval and override rules. A user Seerr does
not know is a refusal, not a request filed by the administrator on their behalf.

**Only a title this caller was served or voted on can be requested.** Without it, the
endpoint is "make my Seerr download any TMDb id", reachable by every household member
and by anything that has borrowed their session. The check is one query against their
own cards and their own votes, and it is the reason ``requestTitle`` takes a title ref
at all rather than a card id: a card expires, and somebody's likes list outlives it.
"""

import logging
from dataclasses import dataclass
from http import HTTPStatus

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.request_backend import RequestBackend, RequestStatus
from tindarr.ports.titles import TitleRef
from tindarr.storage import batches as batch_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.users import User
from tindarr.swipe.engine import SwipeEngine, as_media_user

__all__ = [
    "RequestService",
    "no_backend_user",
    "requests_not_configured",
    "title_not_offered",
]

logger = logging.getLogger(__name__)


def requests_not_configured() -> ProblemError:
    """409: no request backend is configured, so nothing can be asked for."""
    return ProblemError(
        HTTPStatus.CONFLICT,
        "requests_not_configured",
        "No request service is configured on this server.",
    )


def no_backend_user() -> ProblemError:
    """403: the request backend has no account matching this media server user.

    Not "filed as somebody else": a request carries the asker's quota and approval
    rules, and there is nobody here to carry them.
    """
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "no_backend_user",
        "Your media server account has no user on the request service.",
    )


def title_not_offered() -> ProblemError:
    """403: this caller was never shown, and never voted on, that title."""
    return ProblemError(
        HTTPStatus.FORBIDDEN,
        "title_not_offered",
        "You can only request a title the deck has offered you.",
    )


@dataclass(frozen=True, slots=True)
class RequestOutcome:
    """What filing one request produced."""

    status: RequestStatus


class RequestService:
    """Filing one title, with both of the security model's rules applied in order."""

    def __init__(self, engine: AsyncEngine, swipe: SwipeEngine, clock: Clock) -> None:
        self._engine = engine
        self._swipe = swipe
        self._clock = clock

    async def file(self, user: User, ref: TitleRef) -> RequestOutcome:
        """Request one title as this user, or refuse with the reason the contract names."""
        backend = await self._swipe.request_backend()
        if backend is None:
            raise requests_not_configured()
        if not await self.was_offered(user, ref):
            logger.info("a request was refused for a title that was never offered")
            raise title_not_offered()
        return await self._file(user, ref, backend)

    async def auto(self, user: User, ref: TitleRef) -> RequestStatus | None:
        """File a request for a liked card, or return ``None`` if it cannot be filed.

        Auto-request is a convenience on a vote, so nothing it runs into is allowed to
        fail the vote: a backend that is down, an account nobody matched or a quota that
        is spent all come back as "no request happened", and the card stays likeable.
        The offered check is skipped because the caller has just voted on the card,
        which *is* the check.
        """
        backend = await self._swipe.request_backend()
        if backend is None:
            return None
        try:
            return (await self._file(user, ref, backend)).status
        except ProblemError as failure:
            logger.info("an automatic request did not go through", extra={"reason": failure.code})
            return None

    async def was_offered(self, user: User, ref: TitleRef) -> bool:
        """Whether this user was served a card for that title, or voted on it.

        A vote counts on its own because a card is purged a month after it was shown
        and a likes list is not: somebody should still be able to request, in November,
        a film they liked in September.
        """
        async with self._engine.connect() as connection:
            if await vote_repository.get(connection, user.id, ref) is not None:
                return True
            served = await batch_repository.served_refs(connection, user.id)
        return ref in served

    async def _file(self, user: User, ref: TitleRef, backend: RequestBackend) -> RequestOutcome:
        found = await backend.find_user(as_media_user(user))
        if found is None or not found.can_request:
            raise no_backend_user()
        result = await backend.request(ref, found)
        async with write_transaction(self._engine) as connection:
            await vote_repository.mark_requested(
                connection, user.id, ref, status=result.status, now=self._clock.now()
            )
        logger.info("a request was filed", extra={"request_status": result.status})
        return RequestOutcome(status=result.status)
