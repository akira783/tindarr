"""Building one batch: reserve, retrieve, choose, enrich, store.

This is the only place in the server that spends money, and the order of the first two
steps is the reason it is its own module. The day's generation is **charged before the
provider is called**, inside the same write transaction that claimed the job, so two
phones polling together cannot both see "one left"; and it is **not refunded when the
call fails**, because "it failed" is exactly the state a retry loop is in, and a refund
there turns one bad minute at an AI provider into an unbounded number of paid attempts.

What a failure does cost is visible rather than silent: the job row carries the problem
code, the deck reports it once, and the day's ``failures`` count goes up beside the
generation that was spent.

**Enrichment happens here, not when the card is served.** Translation, the region's
streaming offers, the ratings and the trailer are read once for the ten cards of a batch
and stored on the rows; a deck that re-read four services on every poll would cost more
than the batch did. What deliberately stays out of storage is anything that depends on
who is looking or on what has happened since: the "on your services" flag and the
availability badge are computed when the card is served (``tindarr.swipe.deck``), so
ticking a service or filing a request updates cards somebody is already holding.

Nothing here raises at the caller. A generation is background work: it finishes the job
row one way or the other, and the endpoint reads that row.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.connectors import ConnectionCheck
from tindarr.ports.llm import Generation, LlmProvider, LlmProviderKind, Prompt
from tindarr.ports.metadata import Metadata, Provider, RatingsSource, TitleDetails
from tindarr.ports.titles import TitleRef
from tindarr.storage import batches as batch_repository
from tindarr.storage import jobs as job_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import usage as usage_repository
from tindarr.storage.batches import NewCard, StoredProvider, StoredRatings, StoredTrailer
from tindarr.storage.db import write_transaction
from tindarr.storage.users import User
from tindarr.swipe.engine import DECK_SIZE, DeckRequest, Household, SwipeEngine, build_strategy
from tindarr.swipe.strategy import Candidate, StrategyContext

__all__ = ["BatchGenerator", "DeckMode", "GenerationOutcome", "enrich"]

#: What a batch is for, as ``batches.mode`` and the contract's ``Deck.mode`` spell it.
type DeckMode = Literal["normal", "calibration"]

logger = logging.getLogger(__name__)

#: How many titles one batch reads ratings for. OMDb is one call per title and the badge
#: is a nicety; a batch is not worth ten seconds of waiting for three scores.
_RATINGS_TIMEOUT: Final = 10.0


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """What a finished generation produced, for the log and for the tests."""

    job_id: str
    batch_id: str | None = None
    cards: int = 0
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        """Whether a batch was written, empty or not."""
        return self.error_code is None


class BatchGenerator:
    """Runs one user's generation from end to end, and never raises at its caller."""

    def __init__(self, engine: AsyncEngine, swipe: SwipeEngine, clock: Clock) -> None:
        self._engine = engine
        self._swipe = swipe
        self._clock = clock

    async def claim(self, user: User, request: DeckRequest) -> str | None:
        """Take this user's generation slot and charge the day, or return ``None``.

        One transaction, because the three things it does are one decision: is somebody
        already generating for this person, have they any generations left today, and —
        if both answers allow it — take both. Anything less would let two polls a
        millisecond apart each pass a check the other had already invalidated.

        Raises ``DailyLimitReachedError`` when the cap is spent, which is the one
        outcome the caller must tell the user about.
        """
        place = await self._swipe.household()
        limit = _limit_for(user, place)
        async with write_transaction(self._engine) as connection:
            job = await job_repository.claim(connection, user.id, "batch", now=self._clock.now())
            if job is None:
                return None
            await usage_repository.reserve(connection, user.id, limit=limit, now=self._clock.now())
        logger.info("a batch generation was claimed", extra={"novelty": request.novelty})
        return job.id

    async def run(self, user: User, request: DeckRequest, job_id: str) -> GenerationOutcome:
        """Generate one batch for a claimed job. Finishes the job whatever happens."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            await job_repository.start(connection, job_id, now=now)
        try:
            return await self._generate(user, request, job_id)
        except ProblemError as failure:
            return await self.fail(user, job_id, failure.code)

    async def fail(self, user: User, job_id: str, code: str) -> GenerationOutcome:
        """Close a generation that produced nothing, and count what it cost."""
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            await job_repository.fail(connection, job_id, code=code, now=now)
            await usage_repository.record_failure(connection, user.id, now=now)
        logger.info("a batch generation stopped early", extra={"reason": code})
        return GenerationOutcome(job_id=job_id, error_code=code)

    async def _generate(self, user: User, request: DeckRequest, job_id: str) -> GenerationOutcome:
        metadata = await self._swipe.metadata()
        llm = await self._swipe.llm()
        place = await self._swipe.household()
        async with self._engine.connect() as connection:
            preferences = await profile_repository.read_preferences(connection, user.id)
        context = await self._swipe.build_context(user, request, place, preferences)
        mode = _mode(context, request)
        metered = _MeteredProvider(llm)
        strategy = build_strategy(metadata, metered)
        proposed = await strategy.propose(context, DECK_SIZE)
        built = await enrich(
            proposed,
            metadata=metadata,
            ratings=await self._swipe.ratings(),
            region=place.region,
            language=context.language,
        )
        now = self._clock.now()
        async with write_transaction(self._engine) as connection:
            batch_id = await batch_repository.create_batch(
                connection,
                user.id,
                mode=mode,
                novelty=request.novelty,
                media_filter=request.media_filter,
                strategy=strategy.name,
                mood=request.mood,
                now=now,
            )
            await batch_repository.store_cards(connection, batch_id, user.id, built, now=now)
            await job_repository.finish(connection, job_id, result_id=batch_id, now=now)
            await usage_repository.record_tokens(
                connection,
                user.id,
                input_tokens=metered.input_tokens,
                output_tokens=metered.output_tokens,
                now=now,
            )
        logger.info(
            "a batch was generated",
            extra={
                "cards": len(built),
                "mode": mode,
                "strategy": strategy.name,
                "tokens": metered.input_tokens + metered.output_tokens,
            },
        )
        return GenerationOutcome(job_id=job_id, batch_id=batch_id, cards=len(built))


def _limit_for(user: User, place: Household) -> int | None:
    """Return the cap this user is held to: their own, or the server's default.

    A user's own ``0`` is a real answer — the engine is switched off for them — which is
    why the field's absence is ``None`` rather than zero.
    """
    return (
        user.daily_generation_limit
        if user.daily_generation_limit is not None
        else (place.daily_generation_limit)
    )


def _mode(context: StrategyContext, request: DeckRequest) -> DeckMode:
    """Whether this batch calibrates. The context decides unless the caller insisted.

    ``StrategyContext.calibrating`` is what the retrieval layer and the prompt both
    read, so the stored mode is that same property and not a second opinion about it.
    """
    if request.force_calibration is not None:
        return "calibration" if request.force_calibration else "normal"
    return "calibration" if context.calibrating else "normal"


class _MeteredProvider:
    """The configured AI provider, counting what it spent on one batch.

    The harness has the same wrapper for the same reason (``CountingLlmProvider``): the
    strategy owns the call, so the only way to learn what a batch cost without teaching
    the strategy about billing is to count at the port. A provider that fails costs
    nothing here and is counted as a failure by the caller.
    """

    def __init__(self, inner: LlmProvider) -> None:
        self._inner = inner
        self.kind: LlmProviderKind = inner.kind
        self.capabilities = inner.capabilities
        self.input_tokens = 0
        self.output_tokens = 0

    async def test(self) -> ConnectionCheck:
        """Delegate the connection test."""
        return await self._inner.test()

    async def list_models(self) -> list[str]:
        """Delegate the model listing."""
        return await self._inner.list_models()

    async def generate[T: BaseModel](self, prompt: Prompt, schema: type[T]) -> Generation[T]:
        """Delegate the call and add what it cost."""
        answer = await self._inner.generate(prompt, schema)
        self.input_tokens += answer.usage.input_tokens
        self.output_tokens += answer.usage.output_tokens
        return answer


async def enrich(
    proposed: Sequence[Candidate],
    *,
    metadata: Metadata,
    ratings: RatingsSource | None,
    region: str,
    language: str,
) -> list[NewCard]:
    """Turn what the strategy chose into rows, reading the four enrichments once each.

    Every one of them is best effort, and separately so: a card with no trailer is a
    card, a card with no ratings is a card, and a region TMDb will not answer providers
    for costs the badges and nothing else. The one thing a card cannot survive without
    is its details, and the strategy has already dropped the candidates TMDb would not
    describe (``tindarr.swipe.retrieval.card_details``).
    """
    built: list[NewCard] = []
    for candidate in proposed:
        details = candidate.details
        if details is None:
            continue
        built.append(
            NewCard(
                ref=candidate.ref,
                title=details.title,
                pick_type=candidate.pick,
                original_title=details.original_title,
                year=details.year,
                overview=details.overview,
                genres=details.genres,
                runtime_minutes=details.runtime_minutes,
                seasons=details.seasons,
                poster_path=details.poster_path,
                backdrop_path=details.backdrop_path,
                ratings=await _ratings(details, ratings),
                providers=await _providers(metadata, candidate.ref, region),
                trailer=await _trailer(metadata, candidate.ref, language),
                rationale=candidate.reason,
            )
        )
    return built


async def _providers(metadata: Metadata, ref: TitleRef, region: str) -> tuple[StoredProvider, ...]:
    try:
        offers = await metadata.watch_providers(ref, region)
    except ProblemError as failure:
        logger.info("TMDb did not answer watch providers", extra={"reason": failure.code})
        return ()
    return tuple(_as_provider(offer) for offer in offers)


def _as_provider(offer: Provider) -> StoredProvider:
    return StoredProvider(
        provider_id=offer.provider_id,
        name=offer.name,
        offer=offer.offer,
        logo_path=offer.logo_path,
    )


async def _trailer(metadata: Metadata, ref: TitleRef, language: str) -> StoredTrailer | None:
    try:
        found = await metadata.trailer(ref, language)
    except ProblemError as failure:
        logger.info("TMDb did not answer a trailer", extra={"reason": failure.code})
        return None
    if found is None:
        return None
    return StoredTrailer(key=found.key, name=found.name, language=found.language)


async def _ratings(details: TitleDetails, source: RatingsSource | None) -> StoredRatings:
    """TMDb's own score, plus OMDb's three when there is an IMDb id and a key.

    OMDb is asked with the IMDb id TMDb carries, which many series simply do not have.
    A household without an OMDb key gets the TMDb score alone, which is what the
    ``RatingsSource`` port being optional means.
    """
    tmdb = details.vote_average
    if source is None or details.imdb_id is None:
        return StoredRatings(tmdb=tmdb)
    try:
        async with asyncio.timeout(_RATINGS_TIMEOUT):
            found = await source.ratings(details.imdb_id)
    except (ProblemError, TimeoutError):
        logger.info("OMDb did not answer for a card")
        return StoredRatings(tmdb=tmdb)
    if found is None:
        return StoredRatings(tmdb=tmdb)
    return StoredRatings(
        tmdb=tmdb,
        imdb=found.imdb,
        rotten_tomatoes=found.rotten_tomatoes,
        metacritic=found.metacritic,
    )
