"""Checking an environment-set ``public_url`` once, shortly after startup.

An address set from the console is verified before it is saved. One set with
``TINDARR_PUBLIC_URL`` never goes through that path: it is the operator's own value, it
cannot be changed from the console, and refusing to start over it would take a server
down for a setting only pairing uses. So it is checked once, in the background, and a
failure is logged with its coarse reason (docs/auth.md, section 10).
"""

import logging
from datetime import timedelta
from typing import Final

from tindarr.auth.publicurl import PublicUrlVerifier
from tindarr.core.errors import ProblemError
from tindarr.jobs.purge import OneShotJob

#: Long enough for the server to be answering its own address.
PUBLIC_URL_CHECK_DELAY: Final = timedelta(seconds=10)

logger = logging.getLogger(__name__)


def public_url_check_job(verifier: PublicUrlVerifier, public_url: str, host: str) -> OneShotJob:
    """Build the one-shot check of an address the environment forces."""

    async def run() -> None:
        try:
            await verifier.verify(public_url, host)
        except ProblemError as problem:
            logger.warning(
                "TINDARR_PUBLIC_URL did not answer as this server; phones paired with "
                "it may not be able to connect",
                extra={"reason": problem.extensions.get("reason")},
            )
            return
        logger.info("TINDARR_PUBLIC_URL answers as this server")

    return OneShotJob("public_url_check", run, PUBLIC_URL_CHECK_DELAY)
