"""The file a replay reads: a catalogue of titles and a set of votes per user.

Two things live in one document because a metric needs both. The **votes** say what a
person thought of a title; the **catalogue** says what that title is — its genres, its
place in a franchise, how famous it was. Without the catalogue a replay can still score
likes and already-seen, but it cannot say a word about diversity, so the catalogue is
optional and what depends on it is reported as unavailable rather than guessed.

**No dates.** A vote carries a sequence number, not a timestamp. Order is all a replay
needs, and "this person watched this on that evening" is exactly the kind of fact an
anonymised fixture should not carry. Replay turns the sequence back into instants on a
fixed epoch so that the rest of the engine sees the ``Vote`` it expects.

**No names.** A user is a label the fixture chose (``user-1``), never an account id, a
display name or anything that came from a media server.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tindarr.ports.media_server import LibraryIndex, LibraryItem
from tindarr.ports.metadata import Title
from tindarr.ports.titles import MediaKind, TitleRef
from tindarr.swipe.strategy import Novelty, PickKind
from tindarr.swipe.votes import Vote, VoteValue

__all__ = [
    "CatalogEntry",
    "DatasetError",
    "EvalDataset",
    "EvalUser",
    "FixtureVote",
    "OwnedTitle",
    "load_dataset",
]

_FORMAT_VERSION: Final = 1
#: Sequence numbers become instants on this epoch, one minute apart. The date is
#: arbitrary and deliberately not anybody's: only the order carries meaning.
EPOCH: Final = datetime(2020, 1, 1, tzinfo=UTC)


class DatasetError(ValueError):
    """The file is not a vote set this version can read."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CatalogEntry(_Model):
    """One title as the fixture knows it: the ground truth a metric is scored against."""

    tmdb_id: int = Field(gt=0)
    kind: Literal["movie", "tv"]
    title: str
    year: int | None = None
    genres: tuple[str, ...] = ()
    #: What a franchise, collection or series of sequels this belongs to, if any.
    #: Two cards from the same one in a batch is the repetition the harness counts.
    franchise: str | None = None
    popularity: float = 0.0
    original_language: str = "en"
    vote_average: float | None = None
    vote_count: int = 0
    adult: bool = False
    overview: str | None = None

    @property
    def ref(self) -> TitleRef:
        """Return this entry as a title reference."""
        return TitleRef(self.kind, self.tmdb_id)

    def as_title(self) -> Title:
        """Return the ``Title`` a strategy's candidate pool is made of."""
        return Title(
            ref=self.ref,
            title=self.title,
            year=self.year,
            overview=self.overview,
            original_language=self.original_language,
            popularity=self.popularity,
            vote_average=self.vote_average,
            vote_count=self.vote_count,
            adult=self.adult,
        )


class FixtureVote(_Model):
    """One vote, positioned by its rank in the user's history rather than by a date."""

    seq: int = Field(ge=0)
    tmdb_id: int = Field(gt=0)
    kind: Literal["movie", "tv"]
    vote: Literal["like", "dislike", "seen_liked", "seen_disliked", "skip"]
    #: Which kind of pick produced the card the user voted on, when the source knew.
    #: Recorded for the record: the harness never lets a strategy see it.
    pick: Literal["safe", "explore", "calibration"] | None = None

    @property
    def ref(self) -> TitleRef:
        """The title this vote is about."""
        return TitleRef(self.kind, self.tmdb_id)

    def as_vote(self) -> Vote:
        """Return the engine's own ``Vote``, on the fixture's fixed epoch."""
        value: VoteValue = self.vote
        return Vote(at=EPOCH + timedelta(minutes=self.seq), ref=self.ref, value=value)


class OwnedTitle(_Model):
    """One title the household already owned when the votes were cast."""

    tmdb_id: int = Field(gt=0)
    kind: Literal["movie", "tv"]


class EvalUser(_Model):
    """One person's vote history, and the little the engine knew about them."""

    id: str
    votes: tuple[FixtureVote, ...] = ()
    #: What the household already owned when these votes were cast.
    library: tuple[OwnedTitle, ...] = ()
    #: The "Loves / Avoids / Nuances" bullets as they stood. One profile for the whole
    #: history, which is a simplification with teeth: in production the profile is
    #: rewritten every ten votes, so it only ever knows the past, while here it is
    #: whatever the fixture's author wrote — in a generated fixture, the very rule the
    #: later votes were drawn from. A strategy that reads it therefore scores better on
    #: a generated vote set than it would in life (docs/evaluation.md).
    taste_profile: str | None = None
    novelty: Literal["familiar", "balanced", "bold"] = "balanced"
    media_kind: Literal["movie", "tv"] | None = None
    mood: str | None = None

    @property
    def ordered_votes(self) -> tuple[Vote, ...]:
        """Every vote, oldest first."""
        return tuple(row.as_vote() for row in sorted(self.votes, key=lambda row: row.seq))

    @property
    def library_index(self) -> LibraryIndex:
        """The owned titles, as the media server port hands them over."""
        return LibraryIndex(
            LibraryItem(row.kind, f"fixture-{row.kind}-{row.tmdb_id}", "", tmdb_id=row.tmdb_id)
            for row in self.library
        )

    @property
    def novelty_level(self) -> Novelty:
        """The novelty setting these votes were cast under."""
        level: Novelty = self.novelty
        return level

    @property
    def wanted_kind(self) -> MediaKind | None:
        """The media type filter these votes were cast under, if any."""
        return self.media_kind


class EvalDataset(_Model):
    """A whole vote set: who, what they said, and what the titles were."""

    version: Literal[1] = _FORMAT_VERSION
    name: str
    #: One line saying where the votes came from; printed with the metrics, so nobody
    #: reads a number without knowing what it was measured on.
    source: str = ""
    language: str = "en"
    region: str = "US"
    catalog: tuple[CatalogEntry, ...] = ()
    users: tuple[EvalUser, ...] = ()

    @property
    def by_ref(self) -> Mapping[TitleRef, CatalogEntry]:
        """The catalogue, addressed by title. Empty when the fixture carries none."""
        return {entry.ref: entry for entry in self.catalog}

    @property
    def pool(self) -> tuple[Title, ...]:
        """The catalogue as a candidate pool, in a fixed order."""
        return tuple(entry.as_title() for entry in sorted(self.catalog, key=lambda e: e.ref))

    def dump(self) -> str:
        """Return the canonical JSON text of this dataset, newline-terminated."""
        return self.model_dump_json(indent=2) + "\n"

    def write(self, path: Path) -> None:
        """Write the dataset to ``path``, creating the directory if it is missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dump(), encoding="utf-8")

    @classmethod
    def loads(cls, text: str) -> Self:
        """Parse a vote set, refusing a file that is not one."""
        try:
            return cls.model_validate_json(text)
        except ValidationError as failure:
            raise DatasetError(_first_problem(failure)) from None


def _first_problem(failure: ValidationError) -> str:
    errors: Sequence[Mapping[str, object]] = failure.errors(include_url=False)
    if not errors:  # pragma: no cover - pydantic always reports at least one
        return "the vote set is not valid"
    first = errors[0]
    where = ".".join(str(part) for part in (first.get("loc") or ()))
    return f"{where or 'the document'}: {first.get('msg', 'is not valid')}"


def load_dataset(path: Path) -> EvalDataset:
    """Read a vote set from ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as failure:
        raise DatasetError(f"cannot read the vote set: {failure.strerror}") from None
    return EvalDataset.loads(text)


#: Re-exported so callers do not have to know the pick vocabulary lives in the port.
PickKinds: Final[tuple[PickKind, ...]] = ("safe", "explore", "calibration")
