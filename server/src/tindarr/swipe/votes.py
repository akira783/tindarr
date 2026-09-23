"""The vote vocabulary, shared by the engine, the stats and the evaluation harness.

Five values (docs/architecture.md, "Swipe engine"), of which the fork only ever wrote
four: ``skip`` is new here. The groupings below are the only place that says what a vote
*means*, so a metric, a prompt and a stats query can never disagree about whether
``seen_disliked`` is a dislike.

The distinction that matters for the harness is **seen against new**: ``seen_liked`` and
``seen_disliked`` are the user saying "I already know this one", which is exactly the
waste ADR 0013 measured at 47 % of the fork's cards. A strategy that scores well on
likes while proposing titles the user has already watched has not solved the problem, so
the two are counted apart.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args

from tindarr.ports.titles import TitleRef

#: What a user can say about a card.
type VoteValue = Literal["like", "dislike", "seen_liked", "seen_disliked", "skip"]

VOTE_VALUES: Final[tuple[VoteValue, ...]] = get_args(VoteValue.__value__)
#: "More of this", whether or not the title was already known.
POSITIVE_VOTES: Final[frozenset[VoteValue]] = frozenset({"like", "seen_liked"})
#: "Not for me".
NEGATIVE_VOTES: Final[frozenset[VoteValue]] = frozenset({"dislike", "seen_disliked"})
#: "I have already watched this" — the cards a better strategy would not have spent.
SEEN_VOTES: Final[frozenset[VoteValue]] = frozenset({"seen_liked", "seen_disliked"})
#: Cards that were genuinely new to the user when they were shown.
NEW_VOTES: Final[frozenset[VoteValue]] = frozenset({"like", "dislike"})
#: Votes carrying an opinion at all; ``skip`` deliberately carries none.
OPINION_VOTES: Final[frozenset[VoteValue]] = POSITIVE_VOTES | NEGATIVE_VOTES


def as_vote_value(value: object) -> VoteValue | None:
    """Return ``value`` as a vote, or ``None`` when it is not one of the five.

    Vote values arrive from a database that another program wrote (the fork's
    ``swipe_votes`` table), so they are narrowed here rather than trusted.
    """
    return next((vote for vote in VOTE_VALUES if vote == value), None)


@dataclass(frozen=True, slots=True, order=True)
class Vote:
    """One user's answer to one card, with when it was given.

    Ordered by time first so a replay walks a vote set in the order it happened; the
    ref and the value break ties, because two votes recorded in the same second must
    still sort the same way on every machine and in every Python version.
    """

    at: datetime
    ref: TitleRef
    value: VoteValue

    @property
    def seen(self) -> bool:
        """Whether the user said they had already watched this title."""
        return self.value in SEEN_VOTES

    @property
    def positive(self) -> bool:
        """Whether the user wanted more of this."""
        return self.value in POSITIVE_VOTES


def sorted_votes(votes: Iterable[Vote]) -> tuple[Vote, ...]:
    """Return the votes in replay order, oldest first, deterministically."""
    return tuple(sorted(votes))
