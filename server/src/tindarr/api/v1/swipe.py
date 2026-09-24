"""The deck, the votes, and everything a swipe produces.

Thin on purpose. Every rule these endpoints are about — who may see a card, what a vote
is allowed to change, what a request costs and who pays for it — lives in
``tindarr.swipe``, where it can be tested without an HTTP client and where the warm-up
runs the same code. What is here is the shape of the contract and two things that are
genuinely the HTTP layer's:

- **the rate limits**, keyed by the **account** rather than by the address. These are
  post-authentication writes, and behind a reverse proxy or a NAT a whole household
  shares one address: keying by address would let one member spend everybody's budget
  (the same reasoning as the import endpoints, which share the helper);
- **``202`` instead of a body.** A deck with nothing ready raises ``PendingError``, which
  the error layer renders as the contract's ``Pending``. It is raised rather than
  returned so that a poll travelling through four decisions reads the same at each of
  them.

Undo, reset and the profile edit are unsafe methods, so a console session carries its
CSRF token and an ``Origin``; the app's bearer token needs neither. Both reach every
endpoint here, because the deck is the app's whole reason to exist and the console's
swipe page (roadmap 4.6) is about to need all of it.
"""

import logging
from datetime import datetime
from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, status

from tindarr.api.context import RequestContext
from tindarr.api.deps import Services
from tindarr.api.security import Context, Credential, SharedSession
from tindarr.api.v1.models import (
    DeckResponse,
    LikeListResponse,
    LikeResponse,
    PreferencesPatchInput,
    PreferencesResponse,
    ProfileRefreshResponse,
    ProfileSaveInput,
    ProfileStateResponse,
    ProviderListResponse,
    ProviderOptionResponse,
    RequestTitleResponse,
    StatsResponse,
    SwipeStatusResponse,
    TitleRefInput,
    VoteResetResponse,
    VoteResultResponse,
    VoteSubmitInput,
    VoteSubmitResponse,
)
from tindarr.auth import errors
from tindarr.core.errors import ProblemError
from tindarr.ports.deck import MediaFilter, Novelty
from tindarr.ports.titles import MediaKind, TitleRef, as_media_kind
from tindarr.storage.votes import LikeCursor, VoteRecord

router = APIRouter(prefix="/swipe", tags=["swipe"])

logger = logging.getLogger(__name__)

MediaTypePath = Annotated[MediaKind, Path(description="The title's media type.")]
TmdbIdPath = Annotated[int, Path(gt=0, description="The title's TMDb id.")]
MediaFilterQuery = Annotated[MediaFilter | None, Query(description="Films, series or both.")]
NoveltyQuery = Annotated[Novelty | None, Query(description="How far from proven taste.")]
MoodQuery = Annotated[str | None, Query(max_length=200, description="A wish for this session.")]
ModeQuery = Annotated[
    Literal["auto", "normal", "calibration"], Query(description="Force the batch's mode.")
]
LikeStatusQuery = Annotated[
    Literal["all", "to_request", "requested"],
    # Named ``status`` in the contract; ``status`` is FastAPI's own module here.
    Query(alias="status"),
]
CursorQuery = Annotated[str | None, Query(max_length=64, description="From a previous page.")]
LimitQuery = Annotated[int, Query(ge=1, le=100, description="How many to return.")]


def _key(context: RequestContext, session: Credential) -> str:
    """Return the bucket a post-authentication limit counts in: the account."""
    return f"{session.signed_in_user.id}@{context.rate_limit_key}"


@router.get("/status", operation_id="getSwipeStatus", summary="What the deck needs to know")
async def get_status(services: Services, session: SharedSession) -> SwipeStatusResponse:
    """Return what a client must know before it shows its first card."""
    return SwipeStatusResponse.of(await services.deck.status(session.signed_in_user))


@router.get("/deck", operation_id="getDeck", summary="Next cards, or pending while one is built")
async def get_deck(  # noqa: PLR0913 - one parameter per query the contract documents
    services: Services,
    context: Context,
    session: SharedSession,
    *,
    media_type: MediaFilterQuery = None,
    novelty: NoveltyQuery = None,
    mode: ModeQuery = "auto",
    mood: MoodQuery = None,
) -> DeckResponse:
    """Return the cards to show, or ``202`` while a batch is generated.

    The query parameters are a choice for this deck, not a change of mind: they are not
    written back to the preferences, so switching to ``bold`` for one evening does not
    quietly become somebody's setting.
    """
    user = session.signed_in_user
    services.limits.deck.hit(_key(context, session))
    request = await services.deck.resolve(
        user.id,
        media_filter=media_type,
        novelty=novelty,
        mood=mood,
        force_calibration=None if mode == "auto" else mode == "calibration",
    )
    return DeckResponse.of(await services.deck.deck(user, request))


@router.post("/votes", operation_id="submitVotes", summary="Record votes (one or a queue)")
async def submit_votes(
    body: VoteSubmitInput, services: Services, context: Context, session: SharedSession
) -> VoteSubmitResponse:
    """Store each item independently and say what became of it, in the order it came."""
    services.limits.votes.hit(_key(context, session))
    outcomes, refreshed = await services.votes.submit(
        session.signed_in_user, [item.submitted for item in body.votes]
    )
    return VoteSubmitResponse(
        results=[VoteResultResponse.of(outcome) for outcome in outcomes],
        profile_refresh_started=refreshed,
    )


@router.delete("/votes", operation_id="resetVotes", summary="Delete all of the caller's votes")
async def reset_votes(
    services: Services,
    context: Context,
    session: SharedSession,
    confirm: Annotated[Literal["reset-votes"], Query(description="Type the words to confirm.")],
) -> VoteResetResponse:
    """Delete this user's votes. The taste profile is deliberately left standing."""
    services.limits.swipe_writes.hit(_key(context, session))
    return VoteResetResponse(deleted=await services.votes.reset(session.signed_in_user))


@router.delete(
    "/votes/{media_type}/{tmdb_id}",
    operation_id="undoVote",
    summary="Undo the vote on one title",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def undo_vote(
    media_type: MediaTypePath,
    tmdb_id: TmdbIdPath,
    services: Services,
    context: Context,
    session: SharedSession,
) -> None:
    """Remove the caller's answer about one title, whatever it was."""
    services.limits.swipe_writes.hit(_key(context, session))
    removed = await services.votes.undo(session.signed_in_user, TitleRef(media_type, tmdb_id))
    if not removed:
        raise errors.not_found("No vote on that title.")


@router.post("/requests", operation_id="requestTitle", summary="Request a title")
async def request_title(
    body: TitleRefInput, services: Services, context: Context, session: SharedSession
) -> RequestTitleResponse:
    """File a request as the caller's own backend user, for a title they were offered."""
    services.limits.requests.hit(_key(context, session))
    outcome = await services.requests.file(session.signed_in_user, body.ref)
    return RequestTitleResponse(request_status=outcome.status)


@router.get("/likes", operation_id="listLikes", summary="The caller's liked titles")
async def list_likes(
    services: Services,
    session: SharedSession,
    status_filter: LikeStatusQuery = "all",
    cursor: CursorQuery = None,
    limit: LimitQuery = 50,
) -> LikeListResponse:
    """Return the "to request" list, most recent first.

    The cursor is the last like's **whole** ordering key — the time, the media type and
    the id — so a page boundary cannot skip or repeat an entry the way an offset would.
    The time alone is not enough: one offline-queue submission stores every item at the
    same instant, so a queue of sixty likes would page past all but the first fifty.
    """
    found = await services.votes.likes(
        session.signed_in_user, status=status_filter, limit=limit, before=_cursor(cursor)
    )
    return LikeListResponse(
        likes=[LikeResponse.of(entry) for entry in found],
        next_cursor=_next_cursor(found[-1].vote) if len(found) == limit else None,
    )


@router.get("/profile", operation_id="getProfile", summary="The caller's taste profile")
async def get_profile(services: Services, session: SharedSession) -> ProfileStateResponse:
    """Return the profile, whether a rewrite is running, and why the last one failed."""
    return ProfileStateResponse.of(await services.profiles.read(session.signed_in_user))


@router.put(
    "/profile", operation_id="saveProfile", summary="Replace the profile with the user's own text"
)
async def save_profile(
    body: ProfileSaveInput, services: Services, context: Context, session: SharedSession
) -> ProfileStateResponse:
    """Store what the user wrote. A later rewrite adds to it and never replaces it."""
    services.limits.swipe_writes.hit(_key(context, session))
    return ProfileStateResponse.of(await services.profiles.save(session.signed_in_user, body.text))


@router.post(
    "/profile/refresh",
    operation_id="refreshProfile",
    summary="Rewrite the profile in the background",
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_profile(
    services: Services, context: Context, session: SharedSession
) -> ProfileRefreshResponse:
    """Start a rewrite, or say that one is already running.

    ``202`` either way: "somebody is already doing it" is the same news to a client that
    is about to poll the profile, and a conflict would only tell it to try again for a
    thing it wants to have happened.
    """
    services.limits.profile_refresh.hit(_key(context, session))
    started = await services.profile_runner.request_rewrite(session.signed_in_user)
    return ProfileRefreshResponse(started=started)


@router.get("/preferences", operation_id="getPreferences", summary="The caller's deck preferences")
async def get_preferences(services: Services, session: SharedSession) -> PreferencesResponse:
    """Return the preferences, with the server's language where none was chosen."""
    place = await services.swipe.household()
    stored = await services.deck.preferences(session.signed_in_user.id)
    return PreferencesResponse.of(stored, place.language)


@router.patch("/preferences", operation_id="updatePreferences", summary="Change some preferences")
async def update_preferences(
    body: PreferencesPatchInput, services: Services, context: Context, session: SharedSession
) -> PreferencesResponse:
    """Apply a partial change and return the whole of what the deck will now read."""
    services.limits.swipe_writes.hit(_key(context, session))
    place = await services.swipe.household()
    updated = await services.deck.update_preferences(session.signed_in_user.id, body.as_patch())
    return PreferencesResponse.of(updated, place.language)


@router.get(
    "/providers", operation_id="listStreamingProviders", summary="Streaming services in the region"
)
async def list_streaming_providers(
    services: Services, session: SharedSession
) -> ProviderListResponse:
    """Return the region's provider list, from a seven-day cache.

    Stale beats absent: a list a fortnight old is still the right set of logos, and only
    an empty cache turns a TMDb failure into an error.
    """
    region, options = await services.deck.region_providers()
    return ProviderListResponse(
        region=region, providers=[ProviderOptionResponse.of(option) for option in options]
    )


@router.get("/stats", operation_id="getStats", summary="Like and request rates")
async def get_stats(services: Services, session: SharedSession) -> StatsResponse:
    """Return the caller's counts, with ``skip`` outside every rate."""
    return StatsResponse.of(await services.votes.stats(session.signed_in_user))


def _next_cursor(vote: VoteRecord) -> str:
    """Where this page ended, as the whole ordering key."""
    return f"{vote.voted_at.isoformat()}|{vote.ref.kind}|{vote.ref.tmdb_id}"


def _cursor(value: str | None) -> LikeCursor | None:
    """Read a likes cursor, refusing anything that is not one this server issued.

    A caller may send whatever they like, so a value that does not parse is a ``400``
    rather than a query built around ``None`` that quietly returns the first page again
    — which is the shape of bug that makes a client loop for ever.
    """
    if value is None:
        return None
    at, _, rest = value.partition("|")
    kind, _, tmdb_id = rest.partition("|")
    try:
        return LikeCursor(datetime.fromisoformat(at), TitleRef(_kind(kind), int(tmdb_id)))
    except (ValueError, KeyError):
        raise ProblemError(
            HTTPStatus.BAD_REQUEST,
            "validation_error",
            "cursor: not a cursor this server issued",
        ) from None


def _kind(value: str) -> MediaKind:
    """Narrow a cursor's media type, raising the way a bad number in it would."""
    found = as_media_kind(value)
    if found is None:
        raise KeyError(value)
    return found
