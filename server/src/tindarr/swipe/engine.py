"""What every batch needs before it can be asked for: the ports, the settings, the context.

There is one thing in this module that matters more than the rest, and it is the reason
it exists at all rather than being three functions inside the endpoint that needs them:
**the strategy the server serves and the strategy the harness grades must be built the
same way**. ``build_strategy`` is that one construction site. ``tindarr.main.evaluation``
calls it to replay a vote set, and the generation job calls it to serve a household; a
difference between the two would make every number in ``docs/evaluation.md`` a statement
about code nobody runs.

The same goes for the context. ``build_context`` is the server's side of
``StrategyContext``, and the harness's ``replay`` is the other; they are two callers of
one frozen dataclass whose docstring says what a strategy is allowed to know. What this
one adds, and a replay cannot have, is the state of a live household: the media server's
library and engagement, the imports, the grid, the cards already shown, the skips still
inside their cool-down.

**Nothing here decides anything.** It reads. Refusing, charging, storing and answering
belong to the modules above (``generation``, ``deck``, ``voting``), and keeping that line
is what lets the warm-up and a request share every line of this file.
"""

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from http import HTTPStatus
from typing import Final, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from tindarr.connectors import ConnectorService
from tindarr.core.clock import Clock
from tindarr.core.errors import ProblemError
from tindarr.ports.deck import MediaFilter, Novelty
from tindarr.ports.llm import LlmProvider
from tindarr.ports.media_server import (
    Engagement,
    LibraryIndex,
    MediaServer,
    MediaUser,
)
from tindarr.ports.metadata import Metadata, RatingsSource, TitleFilters
from tindarr.ports.request_backend import RequestBackend
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.storage import batches as batch_repository
from tindarr.storage import history as history_repository
from tindarr.storage import profiles as profile_repository
from tindarr.storage import votes as vote_repository
from tindarr.storage.profiles import DeckPreferences
from tindarr.storage.settings import ContentFilters, SettingsStore
from tindarr.storage.users import User
from tindarr.swipe.hybrid import HybridStrategy
from tindarr.swipe.retrieval import POOL_SIZE, PoolSource, Retrieval
from tindarr.swipe.strategy import Strategy, StrategyContext
from tindarr.swipe.votes import Vote, sorted_votes

__all__ = [
    "DECK_LOW_WATER",
    "DECK_SIZE",
    "Household",
    "MediaServerSource",
    "SwipeEngine",
    "as_media_user",
    "build_strategy",
    "household",
    "llm_not_configured",
    "tmdb_not_configured",
]

logger = logging.getLogger(__name__)

#: How many cards one batch asks for. The fork's number, and the one every committed
#: baseline in ``docs/evaluation.md`` was measured at.
DECK_SIZE: Final = 10
#: Below this many showable cards, the deck quietly starts the next batch while it
#: answers. Four is about twenty seconds of swiping, which is more than a generation
#: takes and less than the patience of somebody who has just run out of cards.
DECK_LOW_WATER: Final = 4
#: The default region when the administrator has not set one. TMDb's own.
_DEFAULT_REGION: Final = "US"
#: How long the household's library index is reused. It is one listing of everything the
#: media server holds, it is the same answer for every user, and it changes when
#: somebody adds a film — not between two polls of a deck.
LIBRARY_CACHE: Final = timedelta(minutes=10)
_CACHE_SECONDS: Final = LIBRARY_CACHE.total_seconds()


def tmdb_not_configured() -> ProblemError:
    """409: nothing can be built without TMDb."""
    return ProblemError(
        HTTPStatus.CONFLICT, "tmdb_not_configured", "Configure the TMDb connector first."
    )


def llm_not_configured() -> ProblemError:
    """409: no AI provider is configured, so no batch can be chosen."""
    return ProblemError(
        HTTPStatus.CONFLICT, "llm_not_configured", "Configure an AI provider first."
    )


@dataclass(frozen=True, slots=True)
class Household:
    """What the server's settings mean to the swipe engine. Read, never cached."""

    language: str = "en"
    region: str = _DEFAULT_REGION
    filters: TitleFilters = field(default_factory=TitleFilters)
    daily_generation_limit: int = 10
    warm_up_enabled: bool = True


async def household(settings: SettingsStore) -> Household:
    """Read the language, the region, the filters and the two engine limits."""
    language = (await settings.get("language")).value
    region = (await settings.get("streaming_region")).value
    raw = (await settings.get("content_filters")).value
    limit = (await settings.get("daily_generation_limit")).value
    warm_up = (await settings.get("warm_up_enabled")).value
    stored = ContentFilters.model_validate(raw) if isinstance(raw, dict) else ContentFilters()
    return Household(
        language=language if isinstance(language, str) and language else "en",
        region=region if isinstance(region, str) and region else _DEFAULT_REGION,
        filters=TitleFilters(
            exclude_adult=stored.exclude_adult,
            min_year=stored.min_year,
            excluded_genres=frozenset(stored.excluded_genres),
            excluded_original_languages=frozenset(stored.excluded_original_languages),
        ),
        daily_generation_limit=limit if isinstance(limit, int) and limit >= 0 else 10,
        warm_up_enabled=bool(warm_up),
    )


def build_strategy(
    metadata: Metadata, llm: LlmProvider, pool: PoolSource | None = None
) -> Strategy:
    """Build the strategy ADR 0013 decided on: the one construction site, for everybody.

    The evaluation harness, the live session and the generation job all come through
    here. Before this there were two ``HybridStrategy(...)`` calls and the server would
    have made a third, which is how a pool size or a wiring quietly stops being the one
    the committed baselines were measured at.

    ``pool`` exists for the harness, which wraps the retrieval layer in a watcher so it
    can score what was offered against what was chosen. Everyone else gets the default:
    one retrieval per strategy and one strategy per user, which is what
    ``tindarr.swipe.strategy`` asks for — the retrieval layer caches TMDb's answers for
    the run it is in, and an instance shared between two people is an object that can
    carry one person's pool into another's batch.
    """
    return HybridStrategy(pool or Retrieval(metadata, POOL_SIZE), metadata, llm)


class MediaServerSource(Protocol):
    """How the swipe layer reaches the media server without importing ``tindarr.auth``.

    The two sit side by side in the layer order, so neither may import the other; the
    composition root closes over the connector and hands the result in. ``None`` means
    "not configured", which is a household with no library and no watch history rather
    than an error: the deck works perfectly well for somebody who has only imports.
    """

    async def __call__(self) -> MediaServer | None:
        """Return the media server adapter, or ``None`` when none is configured."""
        ...


@dataclass(frozen=True, slots=True)
class DeckRequest:
    """What a caller asked the deck for, once the preferences have been folded in."""

    media_filter: MediaFilter = "both"
    novelty: Novelty = "balanced"
    mood: str | None = None
    #: ``None`` is the contract's ``auto``: calibration until the vote target is met.
    force_calibration: bool | None = None

    @property
    def media_kind(self) -> MediaKind | None:
        """The kind a strategy is asked for; ``None`` for both."""
        return self.media_filter if self.media_filter in ("movie", "tv") else None


class SwipeEngine:
    """The read side of the swipe engine: ports, settings and one user's whole context."""

    def __init__(
        self,
        engine: AsyncEngine,
        settings: SettingsStore,
        connectors: ConnectorService,
        media_server: MediaServerSource,
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._connectors = connectors
        self._media_server = media_server
        self._clock = clock
        self._library: LibraryIndex | None = None
        self._library_at: float | None = None

    # --- the ports -------------------------------------------------------------------

    async def metadata(self) -> Metadata:
        """Return the TMDb adapter, or refuse: no card exists without it."""
        found = await self._connectors.metadata()
        if found is None:
            raise tmdb_not_configured()
        return found

    async def llm(self) -> LlmProvider:
        """Return the AI provider, or refuse: nothing chooses a batch without it."""
        found = await self._connectors.llm_provider()
        if found is None:
            raise llm_not_configured()
        return found

    async def ratings(self) -> RatingsSource | None:
        """Return the OMDb adapter, or ``None``: the ratings badges are optional."""
        return await self._connectors.ratings()

    async def request_backend(self) -> RequestBackend | None:
        """Return the request backend adapter, or ``None`` when none is configured."""
        return await self._connectors.request_backend()

    async def configured(self) -> tuple[bool, bool]:
        """Whether TMDb and an AI provider are configured, for ``GET /swipe/status``."""
        return (await self._connectors.tmdb()) is not None, (
            await self._connectors.llm()
        ) is not None

    async def llm_name(self) -> str | None:
        """Return the configured provider's kind, shown before the first batch.

        The privacy notice the contract asks for: somebody about to have their taste
        sent to a third party is told which one. The **model** is deliberately not part
        of it — it is an administrator's setting, not a fact about where the data goes.
        """
        found = await self._connectors.llm()
        return found.provider if found is not None else None

    async def household(self) -> Household:
        """Read the household's settings as the engine wants them."""
        return await household(self._settings)

    # --- what the media server knows --------------------------------------------------

    async def library(self) -> LibraryIndex:
        """Return what the household owns, from a short shared cache.

        Best effort on purpose. A media server that is down costs the batch its "we
        already have this" exclusions and its watch links, and nothing else: a deck that
        refused to exist because Jellyfin was restarting would be a worse deck.
        """
        now = self._clock.monotonic()
        fresh = self._library_at is not None and now - self._library_at < _CACHE_SECONDS
        if self._library is not None and fresh:
            return self._library
        adapter = await self._adapter()
        if adapter is None:
            return LibraryIndex()
        try:
            found = await adapter.library_ids()
        except ProblemError as failure:
            logger.info("the media server did not list its library", extra={"reason": failure.code})
            return self._library or LibraryIndex()
        self._library, self._library_at = found, now
        return found

    async def watch_links(self, refs: Sequence[TitleRef]) -> dict[TitleRef, str]:
        """Where to open each title in the media server's own client, when it is owned.

        Only for titles the library holds: a deep link to something nobody has is a
        button that leads to an error page.
        """
        adapter = await self._adapter()
        if adapter is None:
            return {}
        library = await self.library()
        found: dict[TitleRef, str] = {}
        for ref in refs:
            item = library.find(ref)
            link = None if item is None else adapter.deep_link(item)
            if link is not None:
                found[ref] = link
        return found

    async def engagement(self, user: User) -> tuple[Engagement, ...]:
        """Return what this person watched: the media server first, then the imports.

        One tuple, in that order, because the engine reads "they finished this and gave
        up on that" the same way wherever it was learned (ADR 0013's point 4) — and
        because the media server is the source that knows about the last three days.
        """
        from_server = await self._media_engagement(user)
        async with self._engine.connect() as connection:
            imported = await history_repository.engagements(connection, user.id)
        known = {row.ref for row in from_server if row.ref is not None}
        return (*from_server, *(row for row in imported if row.ref not in known))

    async def _media_engagement(self, user: User) -> tuple[Engagement, ...]:
        adapter = await self._adapter()
        if adapter is None or user.media_server_user_id is None:
            return ()
        try:
            return tuple(await adapter.engagement(as_media_user(user)))
        except ProblemError as failure:
            logger.info(
                "the media server did not answer for a user", extra={"reason": failure.code}
            )
            return ()

    async def _adapter(self) -> MediaServer | None:
        try:
            return await self._media_server()
        except ProblemError as failure:
            # Not configured, or something else answers at that address. Either way the
            # deck goes on without a library; the sign-in path is what reports it.
            logger.info("the media server is not usable", extra={"reason": failure.code})
            return None

    # --- one user's whole context ------------------------------------------------------

    async def build_context(
        self,
        user: User,
        request: DeckRequest,
        place: Household,
        preferences: DeckPreferences,
        *,
        batch_index: int = 0,
    ) -> StrategyContext:
        """Assemble everything a strategy is allowed to know about this person.

        The three sets it hands over are worth reading side by side, because they are
        the same idea at three ages:

        - ``voted`` (inside ``history``): an opinion, for ever;
        - ``served``: a card this deck has shown, plus the skips still inside their
          60-day cool-down. A ``skip`` teaches nothing, so it is deliberately **not** a
          vote in ``history`` — which leaves this set as the only thing keeping a title
          somebody said "not now" to out of tomorrow's batch;
        - ``known``: watched somewhere this deck never saw (an import, a grid tick).
        """
        now = self._clock.now()
        async with self._engine.connect() as connection:
            stored = await vote_repository.list_for_user(connection, user.id)
            served = await batch_repository.served_refs(connection, user.id)
            cooling = await vote_repository.skipped_refs(connection, user.id, now=now)
            skipped = await vote_repository.skipped_refs(connection, user.id)
            known = await history_repository.seen_refs(connection, user.id)
            profile = await profile_repository.read_profile(connection, user.id)
        history = _as_votes(stored)
        return StrategyContext(
            user_id=user.id,
            media_kind=request.media_kind,
            novelty=request.novelty,
            mood=request.mood,
            language=preferences.language or place.language,
            region=place.region,
            taste_profile=(profile.text or None) if profile is not None else None,
            history=history,
            library=await self.library(),
            engagement=await self.engagement(user),
            known=known,
            served=_served(served, cooling, skipped),
            filters=place.filters,
            batch_index=batch_index,
            seed=_seed(user.id, batch_index),
        )


def as_media_user(user: User) -> MediaUser:
    """Return the stored user as the media server port wants them named."""
    return MediaUser(
        id=user.media_server_user_id or "",
        name=user.name,
        is_admin=user.media_server_admin,
        remote_access=user.remote_access,
        disabled=not user.enabled,
    )


def _as_votes(stored: Sequence[vote_repository.VoteRecord]) -> tuple[Vote, ...]:
    """Return the vote history a prompt and a replay read: every answer but ``skip``.

    ``skip`` carries no opinion (docs/architecture.md), so it is out of the prompt, out
    of the calibration count and out of every rate. It still has to keep the title off
    the next batch, which ``served`` does instead.
    """
    return sorted_votes(
        Vote(at=row.voted_at, ref=row.ref, value=row.value) for row in stored if row.value != "skip"
    )


def _served(
    served: frozenset[TitleRef], cooling: frozenset[TitleRef], skipped: frozenset[TitleRef]
) -> frozenset[TitleRef]:
    """Return what "already shown" means to the pool, once skips have cooled down.

    A skipped card keeps its row for ever — a vote points at it — so the plain served
    set would keep that title out of the deck long after the 60 days the architecture
    grants it. The skips that have cooled down are therefore taken back out, and the
    ones still inside their window are put in, whether or not the card they came from
    has since been purged.
    """
    return (served - (skipped - cooling)) | cooling


def _seed(user_id: str, batch_index: int) -> int:
    """Return a stable seed for this user and batch, so two identical contexts agree.

    Not ``hash``: Python salts string hashing per process, so a restart would change
    every seed and a strategy that used one would answer differently for no reason a
    reader could see.
    """
    digest = hashlib.blake2b(f"{user_id}:{batch_index}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big")
