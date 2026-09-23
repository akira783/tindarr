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
from typing import Literal

type ConnectorHealth = Literal[
    "ok", "unauthorized", "unreachable", "unexpected_response", "unsupported_version"
]


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    """Result of a connection test: coarse health, never a response body."""

    health: ConnectorHealth
    server_name: str | None = None
    server_version: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the connector works."""
        return self.health == "ok"
