"""``tindarr eval session``: real cards, a real person, one keypress per card.

The replay harness next door compares strategies against a history somebody else's
engine produced. It cannot say whether a deck is *good*, and with a TMDb-wide pool it
cannot even say much about whether it is bad: on the author's 99 votes the fixture
recognises about one card in ten, so the fault counts are a lower bound over a handful
of titles and no amount of tuning moves them.

This is the other half. It builds real batches with the hybrid strategy, against the
real TMDb and a real OpenAI-compatible model, shows one card at a time and reads one
keypress. Every card is answered, so the report at the end has no coverage gap: the
already-seen count is the count, not a floor, and "how many of the new ones did you
want" is a real rate.

Three rules it inherits from the rest of the harness, because a development command that
spends somebody's money by accident is worse than no command:

- **It says what it will call before it calls anything**, and waits for a ``yes`` or a
  ``--yes``. The plan names the address the prompts are sent to.
- **The model endpoint has no default.** It is typed by the operator, and it is always
  the OpenAI-compatible kind; a default that reaches a named vendor is a default that
  bills somebody.
- **The votes are somebody's viewing history.** They go under a ``private/`` directory
  the repository ignores, written after every single answer, and merging them into a
  committed fixture is the person's own decision and nobody else's.
"""

import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, TextIO

import httpx2

from tindarr.adapters.llm.factory import llm_provider_factory
from tindarr.adapters.tmdb import TmdbMetadata
from tindarr.core.errors import ProblemError
from tindarr.main.evaluation import (
    LLM_BASE_VARIABLE,
    EvalError,
    confirm,
    live_key,
    llm_connection,
    private_path,
)
from tindarr.ports.metadata import Metadata, Title
from tindarr.ports.titles import TitleRef
from tindarr.swipe.evaluation import (
    BatchOutcome,
    CostMeter,
    CountingLlmProvider,
    CountingMetadata,
    DatasetError,
    EvaluationReport,
    PoolWatcher,
    ReplayOptions,
    summarize,
)
from tindarr.swipe.evaluation.dataset import CatalogEntry
from tindarr.swipe.evaluation.popularity import profile
from tindarr.swipe.evaluation.session import (
    LEGEND,
    SessionStore,
    Verdict,
    answered,
    catalog_entry,
    score,
)
from tindarr.swipe.hybrid import HybridStrategy
from tindarr.swipe.retrieval import POOL_SIZE, CandidatePool, Retrieval
from tindarr.swipe.strategy import Candidate, Novelty, StrategyContext

__all__ = ["SESSION_FIXTURES", "SessionOptions", "read_key", "run_session", "session_plan"]

logger = logging.getLogger(__name__)

#: Where a session writes, unless told otherwise. Under ``private/``, which
#: ``.gitignore`` keeps out of the repository and ``private_path`` refuses to leave.
SESSION_FIXTURES: Final = Path("fixtures/eval/private/session")
#: The default name of the vote set a session builds.
SESSION_NAME: Final = "private-session"
#: TMDb requests a batch beyond the per-card details: the discovery pages and the
#: recommendation pages the pool is built from, cached for the rest of the session.
POOL_CALLS_PER_BATCH: Final = 8


@dataclass(frozen=True, slots=True)
class SessionOptions:
    """One session's whole configuration."""

    out: Path = SESSION_FIXTURES / "votes.json"
    name: str = SESSION_NAME
    user_id: str = "you"
    batches: int = 3
    batch_size: int = 10
    novelty: Novelty = "balanced"
    language: str = "en"
    region: str = "US"
    #: Whether to ask TMDb which services carry each card. One extra request per card,
    #: and the answer is "included in a subscription somewhere in this region", not "on
    #: yours": the harness opens no instance database, so it has no idea what the
    #: household subscribes to.
    providers: bool = False

    @property
    def cards(self) -> int:
        """How many cards the session will draw at most."""
        return self.batches * self.batch_size


def session_plan(options: SessionOptions, resumed: int) -> str:
    """Say what a session will call, and what it will write, before it does either."""
    import os  # noqa: PLC0415 - read at the moment of use, never cached in a module

    per_card = 2 if options.providers else 1
    requests = options.cards * per_card + options.batches * POOL_CALLS_PER_BATCH
    address = os.environ.get(LLM_BASE_VARIABLE, "").strip() or "(nothing configured)"
    lines = [
        "A live session reaches real services. This one would call:",
        f"  TMDb        up to {requests} requests ({options.batches} batches of "
        f"{options.batch_size}: the details of each card"
        + (", its watch providers" if options.providers else "")
        + ", plus the discovery and recommendation pages the pool is built from, which "
        "are cached for the session). TMDb's API is free for personal use; its rate "
        "limits and caching terms apply.",
        f"  AI provider up to {options.batches} generations, sent to {address}, and one "
        "more for any answer that fails its schema. Whoever owns that address decides "
        "what it costs; the harness cannot know, and a hosted endpoint bills by the "
        "token. Read the price before answering.",
        f"It writes your votes to {options.out}, after every card. That file is your "
        "viewing history: it stays out of the repository unless you put it there.",
    ]
    if resumed:
        lines.append(
            f"{resumed} vote(s) are already in it. They are the history this session "
            "builds on, and the titles they are about will not come back."
        )
    return "\n".join(lines) + "\n"


def read_key(stream: TextIO) -> str:
    """Read one keypress, or one line when there is no terminal to press a key at.

    A terminal is put into raw mode for exactly one character, so a session reads like a
    deck rather than like a form. Anything else — a pipe, a test, a script — reads a line
    and takes its first character, which is what makes the loop drivable without a tty.
    An empty line is a space, so "just press enter" skips.
    """
    if not stream.isatty():
        line = stream.readline()
        if not line:
            return "q"
        stripped = line.rstrip("\n")
        return stripped[0] if stripped else " "
    return _one_keypress(stream)  # pragma: no cover - needs a terminal


def _one_keypress(stream: TextIO) -> str:  # pragma: no cover - needs a terminal
    import termios  # noqa: PLC0415 - POSIX only, and only on the interactive path
    import tty  # noqa: PLC0415

    descriptor = stream.fileno()
    before = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor)
        return stream.read(1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, before)


@dataclass
class _Deck:
    """What the loop carries from one card to the next."""

    store: SessionStore
    meter: CostMeter
    watcher: PoolWatcher
    #: Every card shown so far, as the vote set describes it. The diversity metrics are
    #: computed over it, so they read the catalogue and not what the strategy claimed.
    catalog: dict[TitleRef, CatalogEntry] = field(default_factory=dict[TitleRef, CatalogEntry])


async def run_session(
    options: SessionOptions,
    *,
    confirmed: bool = False,
    out: TextIO = sys.stdout,
    keys: TextIO | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> EvaluationReport:
    """Run a live swipe session and return the report of what the person answered.

    ``transport`` is the one seam: a test drives the whole command against fake services
    through it, exactly as the rest of the harness drives its "live" paths. Left alone it
    is the real network, because a session has no offline mode — a recorded card is not a
    card anybody would answer honestly twice.
    """
    target = private_path(options.out)
    store = _store(target, options)
    out.write(session_plan(options, len(store.user.votes)))
    confirm(confirmed=confirmed)
    connection = llm_connection(live=True)
    meter = CostMeter()
    wire = transport or httpx2.AsyncHTTPTransport()
    metadata = CountingMetadata(TmdbMetadata(live_key(), transport=wire), meter)
    llm = CountingLlmProvider(llm_provider_factory(wire)(connection), meter)
    watcher = PoolWatcher()
    strategy = HybridStrategy(watcher.watching(Retrieval(metadata, POOL_SIZE)), metadata, llm)
    outcomes: list[BatchOutcome] = []
    try:
        outcomes = await _deal(strategy, metadata, _Deck(store, meter, watcher), options, out, keys)
    finally:
        if transport is None:
            await wire.aclose()
    report = summarize(
        store.dataset.name,
        store.dataset.source,
        strategy.name,
        outcomes,
        ReplayOptions(batch_size=options.batch_size, warm_up=0, max_batches=options.batches),
    )
    out.write("\n" + report.table())
    out.write(_verdict_lines(report, target))
    return report


def _store(target: Path, options: SessionOptions) -> SessionStore:
    try:
        return SessionStore.open(
            target,
            name=options.name,
            user_id=options.user_id,
            language=options.language,
            region=options.region,
            novelty=options.novelty,
        )
    except DatasetError as failure:
        raise EvalError(f"{target}: {failure}") from None


async def _deal(  # noqa: PLR0913, PLR0917 - the loop's six collaborators, at one call site
    strategy: HybridStrategy,
    metadata: Metadata,
    deck: _Deck,
    options: SessionOptions,
    out: TextIO,
    keys: TextIO | None,
) -> list[BatchOutcome]:
    """Draw the batches, show the cards, and stop the moment somebody says so."""
    source = keys if keys is not None else sys.stdin
    outcomes: list[BatchOutcome] = []
    for index in range(options.batches):
        before = deck.meter.snapshot()
        cards = await _batch(strategy, deck, options, index, out)
        if not cards:
            out.write("\nThe pool came back empty: there is nothing left to show you.\n")
            break
        pool = deck.watcher.offered(options.user_id, index)
        answers, quit_now = await _answers(
            _Shown(cards, pool, options), deck, metadata, out, source
        )
        if not answers:
            break
        outcomes.append(
            score(
                options.user_id,
                index,
                # A batch somebody walked out of was not a short batch: the strategy
                # returned its cards and nobody looked at the rest. Charging that to
                # `fill_rate`, which is about the strategy, would be a lie about the
                # strategy — so a truncated batch is scored on the cards that were shown.
                len(answers) if quit_now else options.batch_size,
                answers,
                deck.meter.snapshot().since(before),
                profile(pool, [card.ref for card, _ in answers]),
                deck.catalog,
            )
        )
        if quit_now:
            break
    return outcomes


async def _batch(
    strategy: HybridStrategy,
    deck: _Deck,
    options: SessionOptions,
    index: int,
    out: TextIO,
) -> Sequence[Candidate]:
    context = StrategyContext(
        user_id=options.user_id,
        novelty=options.novelty,
        language=options.language,
        region=options.region,
        history=deck.store.history,
        served=deck.store.voted,
        batch_index=index,
        seed=index,
    )
    out.write(f"\n--- batch {index + 1} of {options.batches} ---\n")
    try:
        return await strategy.propose(context, options.batch_size)
    except ProblemError as failure:
        raise EvalError(f"the batch could not be built: {failure.detail or failure.code}") from None


@dataclass(frozen=True, slots=True)
class _Shown:
    """One batch on its way to the terminal: the cards, and the pool they came from."""

    cards: Sequence[Candidate]
    pool: CandidatePool | None
    options: SessionOptions

    def offered(self, ref: TitleRef) -> Title | None:
        """Return what the pool said about this card: where its fame is read from."""
        return self.pool.by_ref.get(ref) if self.pool is not None else None


async def _answers(
    shown: _Shown,
    deck: _Deck,
    metadata: Metadata,
    out: TextIO,
    source: TextIO,
) -> tuple[list[tuple[Candidate, Verdict]], bool]:
    """Show each card and read one answer, until the batch ends or somebody quits."""
    answers: list[tuple[Candidate, Verdict]] = []
    for position, card in enumerate(shown.cards):
        offered = shown.offered(card.ref)
        out.write(_card_text(card, offered, position, len(shown.cards)))
        out.write(await _services(metadata, card.ref, shown.options))
        verdict = _read_verdict(out, source)
        if verdict is None:
            out.write("\nStopped. Everything you answered is already on disk.\n")
            return answers, True
        # On the disk before the next card is drawn: that is what resumable means.
        deck.store.record(card, offered, verdict)
        deck.catalog[card.ref] = catalog_entry(card, offered)
        answers.append((card, verdict))
    return answers, False


def _read_verdict(out: TextIO, source: TextIO) -> Verdict | None:
    """Read keypresses until one of them means something. ``None`` means quit."""
    while True:
        out.write(_legend())
        out.flush()
        known, verdict = answered(read_key(source))
        if known:
            return verdict
        out.write("  that key is not one of these.\n")


def _legend() -> str:
    return "  " + "   ".join(f"[{key}] {what}" for key, what in LEGEND) + "\n  > "


def _card_text(card: Candidate, offered: Title | None, position: int, total: int) -> str:
    """One card, as a terminal shows it."""
    details = card.details
    name = details.title if details is not None else (offered.title if offered else "?")
    year = (details.year if details is not None else None) or (offered.year if offered else None)
    genres = ", ".join(details.genres) if details is not None and details.genres else "—"
    rating = _rating(card, offered)
    kind = "series" if card.ref.kind == "tv" else "film"
    lines = [
        "",
        f"[{position + 1}/{total}] {name}{f' ({year})' if year else ''}  ·  {kind}",
        f"  {genres}  ·  {rating}  ·  {card.pick}",
    ]
    if card.reason:
        lines.append(f"  “{card.reason}”")
    return "\n".join(lines) + "\n"


def _rating(card: Candidate, offered: Title | None) -> str:
    details = card.details
    score_of = (details.vote_average if details is not None else None) or (
        offered.vote_average if offered else None
    )
    votes = (details.vote_count if details is not None else 0) or (
        offered.vote_count if offered else 0
    )
    return f"{score_of:.1f}/10 from {votes} people" if score_of else f"{votes} ratings"


async def _services(metadata: Metadata, ref: TitleRef, options: SessionOptions) -> str:
    """Say where a card can be streamed, when the operator asked and paid for the call.

    Never "on your services": this command opens no instance database and talks to no
    media server, so it knows the region's offers and not the household's subscriptions.
    Saying otherwise would be the more useful sentence and the false one.
    """
    if not options.providers:
        return ""
    try:
        offers = await metadata.watch_providers(ref, options.region)
    except ProblemError:
        return "  (TMDb would not say where it streams)\n"
    included = sorted({offer.name for offer in offers if offer.included})
    if not included:
        return f"  not included in any subscription in {options.region}\n"
    return f"  included in {options.region}: {', '.join(included)}\n"


def _verdict_lines(report: EvaluationReport, target: Path) -> str:
    """Return the two sentences the whole session exists to produce."""
    counts = report.counts
    new = counts.likes + counts.dislikes
    seen = counts.seen_liked + counts.seen_disliked
    judged = f"{counts.scored} card" + ("" if counts.scored == 1 else "s")
    wanted = f"{new} that " + ("was" if new == 1 else "were") + " new to you"
    return (
        f"\nYou judged {judged}: {seen} of them you had already seen, and of the "
        f"{wanted}, you wanted {counts.likes}.\n"
        f"Your votes are in {target}, and they are yours: nothing here commits them and "
        "nothing here publishes them.\n"
        "To replay them past the other strategies, record the TMDb answers they need "
        f"once and then walk them:\n"
        f"  TINDARR_EVAL_TMDB_API_KEY=… tindarr eval record --fixtures {target.parent}\n"
        f"  tindarr eval run --fixtures {target.parent} --strategy popular\n"
    )
