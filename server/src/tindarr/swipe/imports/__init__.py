"""File imports: Netflix, IMDb and Letterboxd, as **taste** sources.

[ADR 0013](../../../../../docs/adr/0013-recommendation-engine.md) measured the idea
everybody has first — "import what they watched elsewhere and stop showing it" — against
99 real votes and three real accounts, and it was worth 3 of the 47 already-seen cards.
Nobody's media server knows about a film watched in a cinema fifteen years ago. So the
already-seen problem is answered by the popularity band and by the calibration grid
(``tindarr.swipe.calibration``), and an import is kept for the other thing it carries:
**engagement**. Three series finished, nine in progress and thirty sampled and dropped
is a sentence about somebody's taste that no API returns.

The modules, in the order one upload goes through them:

- ``records`` — the shared vocabulary, and every bound an upload is held to;
- ``files`` — recognising the format and reading it, ZIP included;
- ``netflix`` — the one format that is prose rather than data;
- ``resolve`` — TMDb, with a review queue instead of ``results[0]``;
- ``run`` — merging, and the engagement the ADR is actually after.

Nothing in here reads a clock, a database or the network directly: ``run`` is handed a
``Metadata`` port and a ``now``, which is what lets the whole of it be tested against a
recorded transport and no fixture directory.
"""

from tindarr.swipe.imports.files import detect_and_parse
from tindarr.swipe.imports.records import (
    IMPORT_FORMATS,
    MAX_UPLOAD_BYTES,
    ImportFormat,
    ParsedFile,
    UnreadableImportError,
    WatchedItem,
)
from tindarr.swipe.imports.resolve import CONFIDENT_SIMILARITY, Candidate
from tindarr.swipe.imports.run import (
    ImportOutcome,
    MetadataDownError,
    ReviewEntry,
    resolve_import,
    watched_title,
)

__all__ = [
    "CONFIDENT_SIMILARITY",
    "IMPORT_FORMATS",
    "MAX_UPLOAD_BYTES",
    "Candidate",
    "ImportFormat",
    "ImportOutcome",
    "MetadataDownError",
    "ParsedFile",
    "ReviewEntry",
    "UnreadableImportError",
    "WatchedItem",
    "detect_and_parse",
    "resolve_import",
    "watched_title",
]
