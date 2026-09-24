"""The deck's small vocabulary: why a card is here, how bold it is, what was said to it.

Four string types, and the narrowing functions that turn a column back into one. They
live at the port level for the reason ``tindarr.ports.history`` gives about
``WatchedTitle``: the storage layer **writes** these values and the swipe layer
**reads** them, so a type defined above the storage layer could not be either's.

They are deliberately spelled as the HTTP contract spells them (``PickType``,
``Novelty``, ``MediaFilter``, ``VoteValue``), so nothing is translated between a column,
a prompt and a JSON body. What each value *means* is not here: that belongs to
``tindarr.swipe.votes``, which is where a metric, a prompt and a statistic agree about
whether ``seen_disliked`` is a dislike.

Every narrowing function returns ``None`` rather than raising. These values come back
from a database, and a database is a file an operator can edit: a row that no longer
says one of the listed words is dropped, not trusted.
"""

from typing import Final, Literal, get_args

__all__ = [
    "MEDIA_FILTERS",
    "NOVELTY_LEVELS",
    "PICK_KINDS",
    "VOTE_VALUES",
    "MediaFilter",
    "Novelty",
    "PickKind",
    "VoteValue",
    "as_media_filter",
    "as_novelty",
    "as_pick_kind",
    "as_vote_value",
]

#: Why a card is in the batch, as the fork labels its picks (the contract's ``PickType``).
type PickKind = Literal["safe", "explore", "calibration"]
#: How far from the user's proven taste the batch should reach (the fork's three levels).
#: ADR 0013 makes it drive the adaptive popularity floor.
type Novelty = Literal["familiar", "balanced", "bold"]
#: What a deck is asked for. ``both`` is "films and series, roughly half of each".
type MediaFilter = Literal["both", "movie", "tv"]
#: What a user can say about a card.
type VoteValue = Literal["like", "dislike", "seen_liked", "seen_disliked", "skip"]

PICK_KINDS: Final[tuple[PickKind, ...]] = get_args(PickKind.__value__)
NOVELTY_LEVELS: Final[tuple[Novelty, ...]] = get_args(Novelty.__value__)
MEDIA_FILTERS: Final[tuple[MediaFilter, ...]] = get_args(MediaFilter.__value__)
VOTE_VALUES: Final[tuple[VoteValue, ...]] = get_args(VoteValue.__value__)


def as_pick_kind(value: object) -> PickKind | None:
    """Return ``value`` as a pick kind, or ``None`` when it is not one."""
    return next((pick for pick in PICK_KINDS if pick == value), None)


def as_novelty(value: object) -> Novelty | None:
    """Return ``value`` as a novelty level, or ``None`` when it is not one."""
    return next((level for level in NOVELTY_LEVELS if level == value), None)


def as_media_filter(value: object) -> MediaFilter | None:
    """Return ``value`` as a media filter, or ``None`` when it is not one."""
    return next((wanted for wanted in MEDIA_FILTERS if wanted == value), None)


def as_vote_value(value: object) -> VoteValue | None:
    """Return ``value`` as a vote, or ``None`` when it is not one of the five.

    Vote values also arrive from a database another program wrote (the fork's
    ``swipe_votes`` table, which ``tindarr import suggestarr`` will read), so they are
    narrowed here rather than trusted.
    """
    return next((vote for vote in VOTE_VALUES if vote == value), None)
