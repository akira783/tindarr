"""The vocabulary every side of Tindarr uses to name one film or one series.

A title is identified by its TMDb id and its kind, and by nothing else: the media
server's own item id, the request backend's media id and the AI provider's free text all
get resolved to a ``TitleRef`` before they travel any further. That is what lets the
swipe engine compare "what the library already holds", "what this user has watched" and
"what the model suggested" without three sets of string matching.

``MediaKind`` uses TMDb's own two words (``movie`` and ``tv``), which the HTTP contract
repeats as ``MediaType``, so nothing has to be translated between the API and the
adapters.
"""

from dataclasses import dataclass
from typing import Literal

type MediaKind = Literal["movie", "tv"]


def as_media_kind(value: object) -> MediaKind | None:
    """Return ``value`` as a media kind, or ``None`` when it is neither."""
    if value == "movie":
        return "movie"
    if value == "tv":
        return "tv"
    return None


@dataclass(frozen=True, slots=True, order=True)
class TitleRef:
    """One film or series, by TMDb id.

    Ordered and hashable so it can key a dictionary (``RequestBackend.status``) and be
    sorted into a stable order for prompts, logs and tests.
    """

    kind: MediaKind
    tmdb_id: int

    def __post_init__(self) -> None:
        """Refuse an id that cannot be a TMDb one, so no caller builds a broken URL."""
        if self.tmdb_id <= 0:
            msg = "a TMDb id is a positive integer"
            raise ValueError(msg)
