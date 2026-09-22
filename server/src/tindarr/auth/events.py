"""Security events, logged as structured records without secrets (docs/auth.md, §13).

One logger (``tindarr.security``) so an operator can route these lines somewhere else.
Fields are ordinary values — a user id, a client address, a reason — never a code, a
token or a user-supplied string; the redaction filter is the second line of defence,
not the first.
"""

import logging
from typing import Final

SECURITY_LOGGER: Final = "tindarr.security"

logger = logging.getLogger(SECURITY_LOGGER)


def security_event(event: str, **fields: str | int | bool | None) -> None:
    """Log one security event at ``INFO``: ``event`` names it, ``fields`` describe it."""
    logger.info("security event", extra={"event": event, **fields})
