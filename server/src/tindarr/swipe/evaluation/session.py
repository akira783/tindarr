"""A live swipe session: the one measurement here that a person actually answers.

Everything else in this package replays votes that were cast on **another engine's**
cards. That is enough to catch a mis-scoring harness and a strategy that is obviously
worse, and it is structurally unable to settle the question ADR 0013 asks — because a
card the recorded engine never showed has no vote, and on the author's 99 votes with a
TMDb-wide pool that is ninety cards in a hundred. No amount of tuning moves a number
measured on a fixture that recognises two per cent of what it is shown.

A session breaks that deadlock the only way it can be broken: it generates real batches
against the real TMDb and a real model, puts the cards in front of somebody one at a
time, and writes down what they say. Every card is then scored — there is no `unknown`
column, no coverage gap, and the avoidance counts stop being a lower bound.

Two things live here, and both are pure:

- ``SessionStore`` — the private vote set the session appends to, in exactly the
  ``EvalDataset`` v1 shape the committed fixtures use, so the harness can replay it the
  moment the session ends and so it could one day be merged into a committed fixture.
  It is written after **every** answer, which is what makes a session resumable: the
  votes of a session that stopped are the history of the next one.
- ``score`` — one batch of live answers, turned into the same ``BatchOutcome`` the
  replay produces, so the session's report is built by ``summarize`` and is the same
  table. A second implementation of the metrics, for the one run that matters most,
  would be the worst possible place to have one.

**What a session cannot say.** ``liked_recall`` has no denominator: nothing was
withheld, so there is no set of titles the strategy could have found and did not. The
report prints it as `n/a` and says so in its notes. What a session does answer is
exactly what the replay cannot — of the cards this person was actually shown, how many
had they already seen, and how many of the new ones did they want.

**It is somebody's viewing history**, so it is written under a ``private/`` directory
the repository ignores, and publishing one is their decision and nobody else's.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Self

from tindarr.ports.metadata import Title, TitleDetails
from tindarr.ports.titles import TitleRef
from tindarr.swipe.evaluation.costs import BatchCost
from tindarr.swipe.evaluation.dataset import (
    CatalogEntry,
    EvalDataset,
    EvalUser,
    FixtureVote,
    load_dataset,
)
from tindarr.swipe.evaluation.popularity import FameSample
from tindarr.swipe.evaluation.replay import BatchOutcome, CardOutcome, diversity
from tindarr.swipe.strategy import Candidate, Novelty
from tindarr.swipe.votes import Vote

__all__ = ["ANSWERS", "LEGEND", "SessionStore", "answered", "catalog_entry", "score"]

#: The five verdicts a vote set stores. A ``CardStatus`` has ten values; the other five
#: are the replay's own vocabulary for cards nobody was asked about, which cannot happen
#: when somebody is sitting in front of the deck answering them.
type Verdict = Literal["like", "dislike", "seen_liked", "seen_disliked", "skip"]

#: What a person may say about a card, by the key they press. ``None`` ends the session.
#:
#: Five verdicts and a quit, which is the vocabulary the app has and the vocabulary a
#: vote set stores. "Seen" is split in two because ADR 0013's whole argument turns on the
#: difference: a title they had already watched **and liked** is the engine reading their
#: taste correctly and wasting the swipe anyway.
ANSWERS: Final[Mapping[str, Verdict | None]] = {
    "l": "like",
    "d": "dislike",
    "s": "seen_liked",
    "x": "seen_disliked",
    " ": "skip",
    "q": None,
}
#: What each key does, in the order the legend prints them.
LEGEND: Final[tuple[tuple[str, str], ...]] = (
    ("l", "like"),
    ("d", "dislike"),
    ("s", "already seen, liked it"),
    ("x", "already seen, disliked it"),
    ("space", "skip"),
    ("q", "quit and keep what you answered"),
)


def answered(key: str) -> tuple[bool, Verdict | None]:
    """Read one keypress: whether it meant anything, and the verdict it meant.

    ``(False, None)`` is a key nobody bound, which reprints the legend rather than
    guessing — a mis-keyed card would otherwise be recorded as somebody's opinion.
    ``(True, None)`` is quit.
    """
    lowered = key.lower()
    return (lowered in ANSWERS, ANSWERS.get(lowered))


@dataclass
class SessionStore:
    """The private vote set a session appends to, rewritten after every answer.

    Rewritten in full rather than appended to, because the file is a document and not a
    log — a few dozen votes is nothing to serialise, and a half-written line in a JSON
    file is a vote set nobody can load. What matters is that it reaches the disk before
    the next card is drawn, so a session that is interrupted keeps everything the person
    said.
    """

    path: Path
    dataset: EvalDataset

    @classmethod
    def open(  # noqa: PLR0913 - a new vote set's whole identity, once
        cls,
        path: Path,
        *,
        name: str,
        user_id: str,
        language: str,
        region: str,
        novelty: Novelty,
        seed_source: str | None = None,
    ) -> Self:
        """Open the vote set at ``path``, or start one if there is nothing there yet.

        An existing file is **read, never reset**: its votes are the history the next
        batch is built from, which is what "resumable" means here. Its stored settings
        win over the ones passed in, so a second session cannot quietly change what the
        first one measured — the command says so and the caller can point somewhere else.

        ``seed_source`` names the fixture, if any, a session was started with
        ``--seed-from``. It is recorded in a brand-new dataset's ``source`` line so that
        the report this session prints, and anyone reading the file later, can see what
        the history was seeded from; it is never written into an existing one, for the
        same reason its other settings are not.
        """
        if path.is_file():
            return cls(path=path, dataset=load_dataset(path))
        source = "a live swipe session, answered by one person at a terminal"
        if seed_source:
            source = f"{source}, seeded from '{seed_source}'"
        return cls(
            path=path,
            dataset=EvalDataset(
                name=name,
                source=source,
                language=language,
                region=region,
                users=(EvalUser(id=user_id, novelty=novelty),),
            ),
        )

    @property
    def user(self) -> EvalUser:
        """The one person this session is about."""
        return self.dataset.users[0]

    @property
    def history(self) -> tuple[Vote, ...]:
        """Every vote already cast, oldest first: the strategy's history."""
        return self.user.ordered_votes

    @property
    def voted(self) -> frozenset[TitleRef]:
        """Every title already answered, in this session or an earlier one."""
        return frozenset(vote.ref for vote in self.user.votes)

    def record(self, card: Candidate, title: Title | None, verdict: Verdict) -> None:
        """Write one answer, and the title it was about, to the disk before returning."""
        entry = catalog_entry(card, title)
        catalog = tuple(row for row in self.dataset.catalog if row.ref != entry.ref)
        vote = FixtureVote(
            seq=len(self.user.votes),
            tmdb_id=card.ref.tmdb_id,
            kind=card.ref.kind,
            vote=verdict,
            pick=card.pick,
        )
        user = self.user.model_copy(update={"votes": (*self.user.votes, vote)})
        self.dataset = self.dataset.model_copy(
            update={"catalog": (*catalog, entry), "users": (user,)}
        )
        self.dataset.write(self.path)


def catalog_entry(card: Candidate, title: Title | None) -> CatalogEntry:
    """Describe one card for the vote set, from the details and the pool between them.

    The details endpoint carries the genres and the rating; the pool's listing carries
    the popularity and the vote count, which is what the fame rows are read from. Taking
    each field from whichever of the two actually has it is the whole of this function.
    """
    details = card.details
    return CatalogEntry(
        tmdb_id=card.ref.tmdb_id,
        kind=card.ref.kind,
        title=_named(details, title, card.ref),
        year=details.year if details is not None else (title.year if title else None),
        genres=details.genres if details is not None else (),
        popularity=title.popularity if title is not None else 0.0,
        original_language=_language(details, title),
        vote_average=_rating(details, title),
        vote_count=_votes(details, title),
        adult=details.adult if details is not None else bool(title and title.adult),
    )


def _named(details: TitleDetails | None, title: Title | None, ref: TitleRef) -> str:
    if details is not None:
        return details.title
    return title.title if title is not None else f"{ref.kind} {ref.tmdb_id}"


def _language(details: TitleDetails | None, title: Title | None) -> str:
    found = (details.original_language if details else None) or (
        title.original_language if title else None
    )
    return found or "en"


def _rating(details: TitleDetails | None, title: Title | None) -> float | None:
    if details is not None and details.vote_average is not None:
        return details.vote_average
    return title.vote_average if title is not None else None


def _votes(details: TitleDetails | None, title: Title | None) -> int:
    if title is not None and title.vote_count:
        return title.vote_count
    return details.vote_count if details is not None else 0


def score(  # noqa: PLR0913, PLR0917 - a batch is scored against six separate facts
    user_id: str,
    index: int,
    requested: int,
    cards: Sequence[tuple[Candidate, Verdict]],
    cost: BatchCost,
    fame: FameSample,
    catalog: Mapping[TitleRef, CatalogEntry],
) -> BatchOutcome:
    """Turn one batch of live answers into the outcome the metrics are computed from.

    There is no ``unknown`` here and there are no wasted statuses: every card was shown
    to somebody who answered it, and the pool had already excluded everything they had
    voted on. A card they quit before reaching is simply not in ``cards``.

    ``liked_available`` is zero on purpose. Recall is "of the titles this person liked,
    how many did the strategy find", and a session withholds nothing, so the question
    has no denominator. The report prints ``n/a`` and says why in its notes; inventing a
    number there would be the one lie this whole package exists to avoid.
    """
    outcomes = tuple(
        CardOutcome(
            ref=card.ref,
            status=status,
            pick=card.pick,
            rank=rank,
            complete=card.details is not None and card.details.ref == card.ref,
        )
        for rank, (card, status) in enumerate(cards)
    )
    spread = diversity(outcomes, catalog)
    return BatchOutcome(
        user_id=user_id,
        index=index,
        requested=requested,
        cards=outcomes,
        cost=cost,
        known=spread.known,
        distinct_genres=spread.distinct_genres,
        franchise_repeat=spread.franchise_repeat,
        liked_available=0,
        fame=fame,
    )
