"""The strategy port: everything that turns "what we know" into "what to show next".

ADR 0013 replaces the fork's engine — the model invents titles, TMDb resolves them —
with a hybrid one where TMDb retrieves and the model picks. Neither ships before the
harness has compared them, so both have to sit behind the same, narrow interface:

    ``propose(context, size) -> Sequence[Candidate]``

``StrategyContext`` is deliberately the *whole* input. A strategy reads nothing else:
no database, no clock, no global. That is what makes an offline replay meaningful — the
harness can hand a strategy the state of the world as it was at some past moment and be
sure that nothing from after that moment leaked in — and it is also what makes a
strategy testable without a running server.

**A strategy may not know the future.** ``history`` holds the votes cast *before* the
batch being asked for, and nothing else. The harness builds those prefixes itself and
scores the proposals against the votes it kept back; a strategy that could see them
would score perfectly and mean nothing.

**A strategy is built per user.** The harness calls a ``StrategyFactory`` once per user
so that no cache can carry one person's answers into another's batch. A strategy that
needs to remember something within a user's run may keep it on the instance.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from tindarr.ports.media_server import Engagement, LibraryIndex
from tindarr.ports.metadata import TitleDetails, TitleFilters
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.votes import NEGATIVE_VOTES, POSITIVE_VOTES, Vote

#: Why a card is in the batch, as the fork labels its picks.
type PickKind = Literal["safe", "explore", "calibration"]
#: How far from the user's proven taste the batch should reach (the fork's three
#: levels). ADR 0013 makes it drive the adaptive popularity floor in step 4.2.
type Novelty = Literal["familiar", "balanced", "bold"]

NOVELTY_LEVELS: tuple[Novelty, ...] = ("familiar", "balanced", "bold")


@dataclass(frozen=True, slots=True)
class Candidate:
    """One proposed card, before it is enriched and stored.

    ``details`` is optional so that a strategy which already had to read TMDb to make
    its choice does not make the caller read it again; a strategy that picked without
    them leaves it ``None``. Nothing downstream trusts it as evidence: the harness
    scores a candidate against its own ground truth, never against what the strategy
    says about its own picks.
    """

    ref: TitleRef
    pick: PickKind = "safe"
    #: One sentence written to the user, in their language. ``None`` until a model
    #: wrote one; the harness does not grade prose.
    reason: str | None = None
    details: TitleDetails | None = None


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is allowed to know when it proposes a batch.

    Built once per batch, by the engine in production and by the harness in a replay.
    Both build it the same way, which is the point: a number the harness prints is a
    number about the code that will actually run.
    """

    user_id: str
    #: ``None`` means "films and series, roughly half of each" (the fork's ``both``).
    media_kind: MediaKind | None = None
    novelty: Novelty = "balanced"
    #: The free text the user typed for this session, already trimmed. Never a filter.
    mood: str | None = None
    #: ISO 639-1, the language rationales and titles are written in.
    language: str = "en"
    #: The region whose streaming providers a card shows.
    region: str = "US"
    #: The "Loves / Avoids / Nuances" bullets, as stored. ``None`` before the first one.
    taste_profile: str | None = None
    #: Every vote cast **before** this batch, oldest first.
    history: tuple[Vote, ...] = ()
    #: What the household already owns.
    library: LibraryIndex = field(default_factory=LibraryIndex)
    #: What this user has watched on the media server.
    engagement: tuple[Engagement, ...] = ()
    #: Cards already served to this user, voted on or not.
    served: frozenset[TitleRef] = frozenset()
    #: What the household refuses to be shown.
    filters: TitleFilters = field(default_factory=TitleFilters)
    #: Which batch of this run it is, counting from zero.
    batch_index: int = 0
    #: A seed a strategy may use where it would otherwise call ``random``. Fixed by the
    #: caller, so two runs of the same strategy on the same inputs agree.
    seed: int = 0

    @property
    def voted(self) -> frozenset[TitleRef]:
        """Every title this user has already voted on."""
        return frozenset(vote.ref for vote in self.history)

    @property
    def liked(self) -> tuple[TitleRef, ...]:
        """The titles this user reacted well to, oldest first."""
        return tuple(vote.ref for vote in self.history if vote.value in POSITIVE_VOTES)

    @property
    def disliked(self) -> tuple[TitleRef, ...]:
        """The titles this user turned down, oldest first."""
        return tuple(vote.ref for vote in self.history if vote.value in NEGATIVE_VOTES)

    @property
    def excluded(self) -> frozenset[TitleRef]:
        """Everything a batch must not contain, for reasons the strategy can see.

        Voted on, already served, or already owned. ADR 0013 asks for these to be
        applied to the candidate pool rather than to the model's answer, which is why
        they are handed to the strategy instead of being filtered out behind its back.
        """
        return self.voted | self.served | self.library.refs


class Strategy(Protocol):
    """One way of choosing the next cards. Implementations live in ``tindarr.swipe``."""

    #: A short stable name; it appears in the harness's output and in its baselines.
    name: str

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        """Return at most ``size`` cards to show next, best first.

        Returning fewer than ``size`` is allowed and is itself measured: a strategy that
        only proposes when it is confident buys its accuracy with an empty deck, and the
        harness reports both numbers so the trade is visible.
        """
        ...


#: Builds one strategy for one user's replay. ``tindarr.main`` and the tests implement it.
type StrategyFactory = Callable[[], Strategy]
