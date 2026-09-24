"""The hybrid strategy: TMDb retrieves the pool, the model chooses from it and explains.

This is the engine ADR 0013 decided on, and it is the fork's engine turned around. In
the fork the model answered from memory and TMDb was asked whether each title existed;
here TMDb answers first (``tindarr.swipe.retrieval``) and the model's whole job is to
choose, order and justify. What that changes, in the order it matters:

- **A hallucinated title is impossible**, so the 1.5x oversample the fork needed to
  survive its own attrition is gone. The batch asks for exactly the cards it wants.
- **The "never propose" list is gone too**, and with it the hope that a model honours
  one. Voted on, already served, owned, filtered out by the household, wrong media
  type: none of it is in the pool, so none of it can come back. What used to be a
  negative list in the prompt is now an absence from the candidate list.
- **The model answers with ids, not titles.** Nothing has to be searched for
  afterwards, and "did it choose one of ours?" is a set membership test rather than a
  fuzzy title match.

**What is carried over from the fork**, because the numbers said it was never the weak
part: the batch prompt's substance (taste profile, what they actually watched and how
hard, recent votes with their verdicts, mood), the safe/explore split, the three novelty
levels with their wording, the "you had already seen a lot of these" nudge, and
calibration mode with both of its variants. The JSON repair and the single validation
retry are carried over too, and live one layer down — every provider adapter already
does them (``tindarr.adapters.llm.structured``), so this module never sees raw text.

**What deliberately did not survive.** The fork re-balanced the model's answer after the
fact, taking three explore cards out of fifteen and interleaving them; the mix now lives
in the *pool* the model chooses from, because reordering an answer the model ordered on
purpose throws away the one thing it was asked to do. And the 200-character mood is
stripped of control characters here rather than passed through raw.

**A model that fails does not cost the batch.** Whatever goes wrong — unreachable,
quota, an answer that never fits the schema — the pool is already a ranked list of cards
this user could be shown, so the batch falls back to it. A deck that stops because an AI
provider had a bad minute is worse than a deck with no sentences under the posters.
"""

import logging
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from tindarr.core.errors import ProblemError
from tindarr.ports.llm import LlmProvider, Prompt
from tindarr.ports.media_server import Engagement, LibraryItem
from tindarr.ports.metadata import Metadata, Title
from tindarr.ports.titles import TitleRef
from tindarr.swipe.retrieval import CandidatePool, PoolSource, card_details
from tindarr.swipe.strategy import (
    Candidate,
    Novelty,
    PickKind,
    StrategyContext,
)
from tindarr.swipe.votes import Vote

__all__ = ["HybridStrategy", "SwipeSelection", "batch_prompt", "series_of"]

logger = logging.getLogger(__name__)

#: Recent votes quoted to the model. Beyond thirty the prompt grows faster than it says
#: anything new.
PROMPT_VOTES: Final = 30
#: Lines of "what they actually watched", strongest first.
PROMPT_ENGAGEMENT: Final = 40
#: Owned titles quoted as taste evidence. The fork listed 150 of them because the list
#: was also the exclusion; the pool excludes them now, so this is evidence only.
PROMPT_LIBRARY: Final = 40
#: Share of a batch asked for as adventurous picks.
EXPLORE_RATIO: Final = 0.3
#: Recent votes needed before "you had already seen a lot of these" is worth saying.
SEEN_RATIO_MIN_VOTES: Final = 10
#: The share of already-seen answers above which that sentence is added.
SEEN_RATIO_WARNING: Final = 0.35
#: How long a mood may be, after trimming. The fork's number.
MOOD_MAX_CHARS: Final = 200
#: How long a rationale may be by the time it reaches a card. Truncated rather than
#: rejected: a sentence that ran long is not a reason to lose a whole batch.
RATIONALE_MAX_CHARS: Final = 400
#: What the model is asked to answer at, when the provider still has a temperature.
TEMPERATURE: Final = 0.8

_VOTE_LABELS: Final = {
    "like": "LIKED",
    "dislike": "DISLIKED",
    "seen_liked": "already seen, LIKED it",
    "seen_disliked": "already seen, DISLIKED it",
    "skip": "not now",
}
_ENGAGEMENT_LABELS: Final = {
    "rewatched": "watched several times",
    "completed": "watched to the end",
    "mostly_watched": "watched most of it",
    "watched": "watched",
    "in_progress": "currently watching",
    "partially_watched": "watched part of it, paused",
    "abandoned": "started, then dropped",
}
_ENGAGEMENT_NOTE: Final = (
    "Weigh this evidence by effort: many episodes of a series or a title watched to the "
    "end is strong; a single film watched once is weak (it may have been casual); a "
    "series started then dropped is a mild negative."
)


class SwipeChoice(BaseModel):
    """One card the model chose, by the id it was given.

    Everything here is untrusted output. The id is looked up in the pool and the card is
    dropped when it is not there; the rationale is truncated and only ever displayed as
    plain text (the security model, section 6).
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    media_type: Literal["movie", "tv"]
    rationale: str
    pick_type: Literal["safe", "explore", "calibration"] = "safe"


class SwipeSelection(BaseModel):
    """One batch, as the model answers it."""

    model_config = ConfigDict(extra="forbid")

    cards: list[SwipeChoice]


class HybridStrategy:
    """Retrieve a pool from TMDb, ask the model to choose from it, build the cards."""

    name = "hybrid"

    def __init__(self, retrieval: PoolSource, metadata: Metadata, llm: LlmProvider) -> None:
        self._retrieval = retrieval
        self._metadata = metadata
        self._llm = llm

    async def propose(self, context: StrategyContext, size: int) -> Sequence[Candidate]:
        """Return up to ``size`` cards, best first, every one of them from the pool."""
        # The pool reads the same property, so every strategy gets the same shortlist
        # for this batch; what changes here is only what the model is asked for.
        calibrating = context.calibrating
        pool = await self._retrieval.pool(context)
        if not pool:
            # Nothing to choose from is not a model's fault and not a model's problem:
            # asking one to pick from an empty list costs a call and answers nothing.
            logger.info("no candidate to propose", extra={"batch": context.batch_index})
            return ()
        chosen = await self._choose(context, pool, size, calibrating=calibrating)
        built = [await self._card(title, pick, reason, context) for title, pick, reason in chosen]
        return [card for card in built if card is not None]

    async def _choose(
        self, context: StrategyContext, pool: CandidatePool, size: int, *, calibrating: bool
    ) -> list[tuple[Title, PickKind, str | None]]:
        try:
            selection = await self._llm.generate(
                Prompt(
                    instructions=INSTRUCTIONS,
                    message=batch_prompt(context, pool, size, calibrating=calibrating),
                    temperature=TEMPERATURE,
                ),
                SwipeSelection,
            )
        except ProblemError as failure:
            # The pool is already a ranked list of cards this user could be shown. A
            # batch without sentences beats no batch, and the operator sees the code.
            logger.warning(
                "the AI provider did not answer; serving the retrieved pool as it stands",
                extra={"reason": failure.code, "batch": context.batch_index},
            )
            return _from_pool(pool, size, [], context.excluded)
        chosen = _within_budget(_one_per_series(_kept(selection.value, pool, context)), pool, size)
        return _from_pool(pool, size, chosen, context.excluded)

    async def _card(
        self, title: Title, pick: PickKind, reason: str | None, context: StrategyContext
    ) -> Candidate | None:
        details = await card_details(self._metadata, title.ref, context.language)
        # A card TMDb will not describe cannot be rendered, so it is dropped rather than
        # served blank — and one of them must not take the other nine with it.
        return (
            None
            if details is None
            else Candidate(ref=title.ref, pick=pick, reason=reason, details=details)
        )


#: The one system line. It says what the answer is, and nothing a candidate title could
#: talk it out of: the schema is what actually holds the shape.
INSTRUCTIONS: Final = (
    "You choose movies and TV shows for a recommendation feed from a list of candidates, "
    "and only output raw JSON objects."
)


def _kept(
    selection: SwipeSelection, pool: CandidatePool, context: StrategyContext
) -> list[tuple[Title, PickKind, str | None]]:
    """Keep the choices that are really in the pool, once each, in the model's order.

    Three ways a choice is dropped, all of them silent and all of them counted by the
    harness rather than argued with: an id that was never offered, the same id twice,
    and — belt and braces — an id the context excludes. The last cannot happen, because
    the pool was filtered before the model saw it; it is checked anyway, since the day
    it can happen is the day a retrieval bug ships a title somebody already voted on.
    """
    offered = pool.by_ref
    excluded = context.excluded
    taken: set[TitleRef] = set()
    kept: list[tuple[Title, PickKind, str | None]] = []
    for choice in selection.cards:
        ref = TitleRef(choice.media_type, choice.id) if choice.id > 0 else None
        title = offered.get(ref) if ref is not None else None
        if title is None or ref is None or ref in taken or ref in excluded:
            continue
        taken.add(ref)
        kept.append((title, choice.pick_type, _sentence(choice.rationale)))
    return kept


def _one_per_series(
    chosen: Sequence[tuple[Title, PickKind, str | None]],
) -> list[tuple[Title, PickKind, str | None]]:
    """Keep one title per series in a batch, whatever the prompt asked for.

    The prompt says not to serve two of the same franchise, and the harness measures how
    often it happens anyway: often enough that asking is not a mechanism. A deck of ten
    that is three sequels of one thing is a deck of eight.

    The signal is the one a listing gives: a title before its colon. TMDb's list
    endpoints carry no collection — only the detail endpoint does, and reading details
    for sixty candidates to build one batch would cost more than the batch — so this
    groups ``Dune: Part Two`` with ``Dune: Prophecy`` and leaves a title without a colon
    alone. It can group two unrelated films whose names happen to start the same way;
    the cost of that is one card swapped for the next in the pool.
    """
    kept: list[tuple[Title, PickKind, str | None]] = []
    series: set[str] = set()
    for entry in chosen:
        name = series_of(entry[0].title)
        if name is not None and name in series:
            continue
        if name is not None:
            series.add(name)
        kept.append(entry)
    return kept


class _FameBudget:
    """How many cards of one batch may come from the most-rated quarter of the pool.

    The measurement that asked for this is in ``docs/evaluation.md``: given the pool,
    the model's picks sat above their own pool's vote-count median in eight batches of
    nine, while both floors sat below theirs. The vote count is how many people ever had
    an opinion about a title, which is the closest thing TMDb carries to "they have
    probably already seen it" — and ADR 0013's whole complaint is that 47 % of the
    fork's cards were titles the user already knew.

    It is spent here, on the batch, rather than filtered out of the pool. A candidate
    the novelty band admits is one this user could legitimately be shown; removing it
    from the pool would remove it from *every* batch of the run, and the earlier attempt
    at that — a fame cut on the popularity axis — cost recall and moved nothing.
    """

    __slots__ = ("_left", "_threshold")

    def __init__(self, pool: CandidatePool, size: int) -> None:
        self._threshold = pool.famous_votes
        self._left = pool.famous(size)

    def spend(self, title: Title) -> bool:
        """Whether ``title`` fits the budget, charging it when it does."""
        if title.vote_count <= self._threshold:
            return True
        if self._left <= 0:
            return False
        self._left -= 1
        return True


def _within_budget(
    chosen: Sequence[tuple[Title, PickKind, str | None]], pool: CandidatePool, size: int
) -> list[tuple[Title, PickKind, str | None]]:
    """Keep the model's order, dropping the picks past the batch's fame budget.

    Asking is not a mechanism — the franchise rule learned that the expensive way — so
    the prompt states the budget *and* the answer is held to it. What is dropped is
    replaced from the pool, so the batch is the same size and the model has spent its
    own choices on blockbusters rather than bought extra cards with them.
    """
    budget = _FameBudget(pool, size)
    return [entry for entry in chosen if budget.spend(entry[0])]


#: Shorter than this, a prefix is a word like "The" and groups nothing meaningful.
_SERIES_PREFIX: Final = 4


def series_of(title: str) -> str | None:
    """Return the part of a title before its colon, when it is worth grouping on."""
    head = title.partition(":")[0].strip().casefold() if ":" in title else ""
    return head if len(head) >= _SERIES_PREFIX else None


def _sentence(text: str) -> str | None:
    """Return a rationale fit to show: one line, trimmed, bounded."""
    cleaned = " ".join(text.split())
    return cleaned[:RATIONALE_MAX_CHARS] or None


def _from_pool(
    pool: CandidatePool,
    size: int,
    chosen: Sequence[tuple[Title, PickKind, str | None]],
    excluded: frozenset[TitleRef],
) -> list[tuple[Title, PickKind, str | None]]:
    """Top a short selection up from the head of the pool, in the pool's own order.

    A model that answers with four cards for a batch of ten has not been more careful,
    it has left the deck half empty — and the harness grades ``fill_rate`` for exactly
    that reason. The cards added here carry no rationale, which is visible on the card
    and is the honest version of "the model did not say".

    ``excluded`` is re-applied here as well as to the model's answer. It is the same
    belt-and-braces check, on the path that is *easiest* to forget: the fallback runs
    when the model failed, which is the moment nobody is watching. So is the one-per-
    series rule: a fill that put the sequel back would undo it silently. So is the fame
    budget: a fill that put the blockbuster back would undo that one too.

    The budget is spent on the second pass and not on a card that would leave the deck
    short. A rule of our own that empties a batch is worse than the fame it was written
    against, and ``fill_rate`` grades exactly that.
    """
    filled = list(chosen)
    taken = {title.ref for title, _, _ in filled}
    series = {name for title, _, _ in filled if (name := series_of(title.title)) is not None}
    budget = _FameBudget(pool, size)
    for title, _, _ in filled:
        budget.spend(title)
    for famous in (False, True):
        for title in pool.titles:
            if len(filled) >= size:
                break
            name = series_of(title.title)
            if title.ref in taken or title.ref in excluded:
                continue
            if name is not None and name in series:
                continue
            if not famous and not budget.spend(title):
                continue
            taken.add(title.ref)
            if name is not None:
                series.add(name)
            filled.append((title, pool.origin.get(title.ref, "safe"), None))
    return filled[:size]


# --- the prompt -----------------------------------------------------------------------


def batch_prompt(
    context: StrategyContext, pool: CandidatePool, size: int, *, calibrating: bool
) -> str:
    """Build the user message: the evidence, the instruction, and the candidate list.

    Deterministic, top to bottom. Two runs with the same context and the same pool
    produce the same bytes, which is what lets a recorded answer be replayed.
    """
    return "\n\n".join(
        part
        for part in (
            _OPENING,
            _evidence(context),
            _picking(context, size, calibrating=calibrating),
            _candidates(pool),
            _fame_budget(pool, size),
            _rules(context, size),
        )
        if part
    )


_OPENING: Final = (
    "You choose recommendation cards for a personal media server. Cards are shown one at "
    'a time; the user answers like, dislike, or "already seen" (liked / disliked).'
)


def _evidence(context: StrategyContext) -> str:
    """Build the prompt's evidence sections, in a fixed order.

    ``taste_profile`` goes in as it stands, newlines and all — it is three headed lists
    of bullets and flattening it would destroy the shape the model is meant to read. It
    is also the one section written by a *model* (and edited by the user), which means a
    prompt this layer builds contains text an earlier prompt produced. Nothing downstream
    trusts it: the answer is a set of ids checked against the pool, and the pool was
    filtered before the model saw it.
    """
    sections: list[str] = []
    if context.taste_profile:
        sections.append(
            f"TASTE PROFILE (maintained from the user's votes):\n{context.taste_profile}"
        )
    watched = _engagement_lines(context.engagement)
    if watched:
        sections.append(
            "WHAT THE USER HAS ALREADY WATCHED, on their media server or in a history "
            "they imported (strongest signals "
            f"first):\n{watched}\n{_ENGAGEMENT_NOTE}"
        )
    votes = _vote_lines(context.history)
    if votes:
        sections.append(f"RECENT VOTES on previous cards (newest first):\n{votes}")
    owned = _library_lines(context)
    if owned:
        sections.append(f"ALREADY IN THEIR LIBRARY, which is taste evidence too:\n{owned}")
    mood = _mood(context.mood)
    if mood:
        sections.append(f'WHAT THE USER FEELS LIKE RIGHT NOW: "{mood}"')
    return "\n\n".join(sections) or "No history yet."


def _vote_lines(history: Sequence[Vote]) -> str:
    """Return the recent votes, by TMDb id and verdict.

    **By id, because a ``Vote`` carries no title.** The fork wrote "Blade Runner (movie,
    1982): LIKED" here, which a model can reason about; this writes "movie 78: LIKED",
    which it mostly cannot. The titles exist — in the stored batch the card came from —
    and serving them is roadmap 4.5's job, not this layer's: resolving thirty ids against
    TMDb on every batch would cost more calls than the batch itself. Until then the taste
    profile carries the burden of saying what this person likes, which is what it is for.
    """
    recent = list(reversed(history))[:PROMPT_VOTES]
    return "\n".join(
        f"- {vote.ref.kind} {vote.ref.tmdb_id}: {_VOTE_LABELS.get(vote.value, vote.value)}"
        for vote in recent
    )


def _engagement_lines(engagement: Sequence[Engagement]) -> str:
    rows = list(engagement)[:PROMPT_ENGAGEMENT]
    return "\n".join(
        f"- {_named(row.item)}: {_ENGAGEMENT_LABELS.get(row.state, row.state)}{_detail(row)}"
        for row in rows
        if row.ref is not None
    )


def _named(item: LibraryItem) -> str:
    """One owned or watched title, as the fork wrote it: ``Dark (tv, 2017)``.

    A row with no name renders as ``" (tv, )"``, which is worth nothing to a model.
    Nothing the engine produces is nameless — imported history carries the title TMDb
    matched (``tindarr.ports.history``) and the library carries the media server's — but
    a *fixture's* library does, and giving it a fallback here would change the prompt
    and therefore re-measure a committed baseline for a change nobody can show. It is
    the fixture that should carry its titles; roadmap 4.5 owns that side.
    """
    year = f", {item.year}" if item.year else ""
    return f"{item.name} ({item.kind}{year})"


def _detail(row: Engagement) -> str:
    if row.episodes_total:
        return f" ({row.episodes_played or 0}/{row.episodes_total} episodes)"
    if row.episodes_played:
        return f" ({row.episodes_played} episodes)"
    return f" ({round(row.progress * 100)}% watched)" if row.progress else ""


def _library_lines(context: StrategyContext) -> str:
    owned = sorted(context.library.items, key=lambda item: (item.kind, item.name, item.item_id))
    return "\n".join(f"- {_named(item)}" for item in owned[:PROMPT_LIBRARY])


def _mood(mood: str | None) -> str | None:
    """Trim a mood to one bounded line of printable text.

    It is the one piece of the prompt the user writes, so it is the one piece an
    injection would arrive in. Nothing here can stop a sentence being persuasive — the
    defence against that is the schema and the pool, neither of which the model can
    widen — but the newlines that would let it forge a section header do not survive.
    """
    if mood is None:
        return None
    printable = "".join(character if character.isprintable() else " " for character in mood)
    return " ".join(printable.split())[:MOOD_MAX_CHARS].strip() or None


def _picking(context: StrategyContext, size: int, *, calibrating: bool) -> str:
    if calibrating:
        return _calibration(context, size)
    explore = max(1, round(size * EXPLORE_RATIO))
    return (
        f"Choose {size} titles: about {size - explore} SAFE picks squarely matching the "
        f'profile (pick_type "safe") and about {explore} ADVENTUROUS picks (pick_type '
        '"explore"): a neighbouring genre, another country or era, still plausible for '
        "this user but a stretch. Weigh votes on adventurous picks heavily: a liked one "
        "widens the taste, a disliked one marks a boundary. "
        f"{_novelty(context.novelty, _seen_ratio(context.history))}"
    )


def _calibration(context: StrategyContext, size: int) -> str:
    if context.history:
        return (
            f"This batch recalibrates the profile of a user who already answered the cards "
            f"above. Choose {size} well-known titles in genres, tones, eras and countries "
            "their answers do NOT cover yet, so each answer teaches something new; skip the "
            "most obvious blockbusters they have probably been shown already. Set pick_type "
            'to "calibration" for every card.'
        )
    return (
        f"The user is new, so this batch calibrates their profile. Choose {size} VERY "
        "well-known titles that most people have seen or heard of: big hits and classics "
        "from different decades, deliberately spread across contrasting genres, tones and "
        'countries, so each answer reveals something new. Set pick_type to "calibration" '
        "for every card."
    )


def _novelty(novelty: Novelty, seen_ratio: float | None) -> str:
    """Return the fork's novelty sentence, kept word for word.

    They now sit beside a pool that has already been filtered to the same intent
    (``tindarr.swipe.retrieval.NOVELTY_BANDS``), so the sentence steers the choice within
    a band rather than being the whole of the setting, which is what it was in the fork.
    """
    if novelty == "familiar":
        return (
            "NOVELTY: favour well-known, popular, highly rated titles squarely in their "
            "taste; comfort picks are welcome."
        )
    note = ""
    if seen_ratio is not None and seen_ratio >= SEEN_RATIO_WARNING:
        note = (
            f" They had already seen {round(seen_ratio * 100)}% of the recent cards: avoid "
            "the obvious hits of each genre."
        )
    if novelty == "bold":
        return (
            "NOVELTY: favour hidden gems: lesser-known and under-seen titles, recent "
            "releases, international productions, cult favourites; avoid blockbusters and "
            f"titles most people have seen.{note}"
        )
    return (
        "NOVELTY: mix a few well-known titles with less obvious ones they are unlikely to "
        f"have seen.{note}"
    )


def _seen_ratio(history: Sequence[Vote]) -> float | None:
    """Return the share of recent answers that were already-seen, or ``None``.

    Suppressed below ten recent votes, as in the fork: three answers out of three is not
    a ratio, and telling a model that somebody has seen 100 % of their cards on that
    evidence changes a whole deck for nothing.
    """
    recent = list(reversed(history))[:PROMPT_VOTES]
    if len(recent) < SEEN_RATIO_MIN_VOTES:
        return None
    return sum(1 for vote in recent if vote.seen) / len(recent)


def _candidates(pool: CandidatePool) -> str:
    lines = "\n".join(_candidate_line(title, pool.genre_names(title)) for title in pool.titles)
    return (
        "CANDIDATES — the only titles you may choose, one per line as "
        f'"id | title (type, year) [genres] rating/number of people who rated it":\n{lines}'
    )


def _fame_budget(pool: CandidatePool, size: int) -> str:
    """Say what the vote count means and how much of the batch may spend it.

    The budget was a silent pool filter before this, which is the shape of defect the
    harness caught: every candidate had already passed the adaptive popularity floor, so
    the floor could never be read off a card, and the one number on the line that *does*
    track "they have already seen this" — the vote count — was printed with no
    explanation at all. The model duly ranked by it.

    The threshold is the pool's own median rather than a constant, so it means the same
    thing whatever TMDb answered today, and the sentence is only written when there is a
    budget to state: at the comfort end, and on a calibration batch, famous is the point.
    """
    allowed = pool.famous(size)
    if allowed >= size or not pool.titles:
        return ""
    return (
        "HOW FAMOUS A CANDIDATE IS. The number after the slash is how many people have "
        'ever rated that title on TMDb. It is the best signal here for "this user has '
        'probably already seen it": nearly half the cards the previous engine served '
        "this user were titles they already knew, and they were the most-rated ones.\n"
        f"A quarter of the candidates above are rated by more than {pool.famous_votes} "
        f"people. At most {allowed} of your {size} cards may come from that quarter; the "
        "rest must be less-rated titles. Cards past that limit are dropped and filled from the "
        "list, so a deck of blockbusters costs you your own choices, not extra cards."
    )


def _candidate_line(title: Title, genres: Sequence[str]) -> str:
    """One candidate, in the fields a taste can be matched against.

    The genres are here because without them the line is a title string and a year: a
    model recognises a famous film from that and nothing else, which is the memory ADR
    0013 is trying to stop depending on. The rating and the vote count are what "is this
    worth a card, and is it something everybody has seen" is read from.
    """
    year = f", {title.year}" if title.year else ""
    named = f" [{', '.join(_line(genre) for genre in genres)}]" if genres else ""
    rating = f"{title.vote_average:.1f}" if title.vote_average is not None else "?"
    return (
        f"- {title.ref.tmdb_id} | {_line(title.title)} ({title.ref.kind}{year})"
        f"{named} {rating}/{title.vote_count}"
    )


def _line(text: str) -> str:
    """Flatten remote text to one line, so nothing TMDb carries can forge another.

    TMDb's catalogue is community-edited, and a title holding a newline would otherwise
    write its own ``- <id> | …`` entry, or its own ``Rules:`` block, into the prompt. An
    id that was never in the pool is dropped a moment later whatever the prompt said, so
    this is the second lock rather than the first — and it costs one call.
    """
    return " ".join(text.split())


def _rules(context: StrategyContext, size: int) -> str:
    return (
        "Rules:\n"
        f"- Choose exactly {size} cards, and only from the candidate list above. Copy the "
        "id exactly; a card whose id is not in the list is dropped, and the user sees one "
        "card fewer.\n"
        '- media_type is the type shown beside the title ("movie" or "tv").\n'
        '- rationale: ONE short sentence written to the user ("you"), in the language '
        f'with ISO code "{context.language}", citing concrete evidence (a title they liked '
        "or watched, a stated preference). For adventurous picks, say why the stretch is "
        "worth it.\n"
        "- No duplicates, and never two titles from the same franchise or series in one "
        "batch. Vary genres within the batch. Best card first.\n\n"
        'Return ONLY a JSON object: {"cards": [{"id": 27205, "media_type": "movie", '
        '"rationale": "...", "pick_type": "safe"}]}'
    )
