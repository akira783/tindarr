"""What every connector answers when it is tested, and nothing more.

A connection test is the one operation an administrator can aim at an address of their
choosing, so its result is deliberately poor: a coarse health value and, at most, the
remote product's own name and version. No status code, no response body, no error
message from the other side (the security model, section 7, "no reflection"). That is
what stops the test from being turned into a reader of internal pages.

``ConnectorHealth`` is shared by the media server, the metadata services, the request
backend and the AI providers, so the console renders one badge for all of them.
"""

from dataclasses import dataclass
from typing import Final, Literal

type ConnectorHealth = Literal[
    "ok", "unauthorized", "unreachable", "unexpected_response", "unsupported_version"
]

#: Longest remote product name or version this carries. A product name is short; four
#: megabytes of prose from an address somebody typed is not a product name.
MAX_LABEL_LENGTH: Final = 64


def clean_label(value: str | None) -> str | None:
    """Return a remote service's own words, reduced to what a label can be.

    These two fields are the only thing a connection test is allowed to carry out of a
    remote answer, and they are the only words in it that Tindarr did not write. So they
    are cut to a plausible length and stripped of anything that is not printable: a
    service at an address an administrator chose must not be able to put control
    characters, line breaks or a kilobyte of text into somebody's console — nor to use
    this field as a way of reading its own body back out
    (the security model, section 7).
    """
    if value is None:
        return None
    kept = "".join(character for character in value.strip() if character.isprintable()).strip()
    return kept[:MAX_LABEL_LENGTH] or None


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """Result of a connection test: coarse health, never a response body."""

    health: ConnectorHealth
    server_name: str | None = None
    server_version: str | None = None

    def __post_init__(self) -> None:
        """Reduce the two remote fields to what a label can be, wherever they came from.

        Done here rather than in each adapter, so a new adapter cannot forget.
        """
        object.__setattr__(self, "server_name", clean_label(self.server_name))
        object.__setattr__(self, "server_version", clean_label(self.server_version))

    @property
    def ok(self) -> bool:
        """Whether the connector works."""
        return self.health == "ok"
