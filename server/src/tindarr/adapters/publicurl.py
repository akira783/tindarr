"""Calling this server's own public address, to check ``public_url`` before saving it.

The one outbound call Tindarr makes to an address an administrator typed, so it is the
narrowest client in the code base (the security model, section 7, SSRF):

- **no redirect is ever followed.** A redirect is a failure (``redirected``), not a
  hop: following one would let a typo point the check at a server that happens to
  answer, and hand it the nonce;
- **TLS is always verified**, whatever the media server connector's own setting;
- **five seconds for the whole call**, not per chunk. An address that trickles one byte
  every four seconds would otherwise hold an administrator's request open forever, and
  the host it was typed as with it;
- the body is read as a stream and abandoned past 64 KiB — far more than the answer
  needs — so the same address cannot make the process grow. Only ``public_url_proof``
  is read from it, and no response body, status or address ever leaves this module.
"""

import asyncio
import logging
from http import HTTPStatus
from typing import Final

import httpx2

from tindarr.adapters.http import HttpSession, RemoteCallError, as_text, read_mapping
from tindarr.ports.publicurl import PROOF_FIELD, VERIFY_NONCE_HEADER, ProofResponse

#: The path the proof is read from, on the public address.
SERVER_INFO_PATH: Final = "/api/v1/server/info"
#: Shorter than every other outbound call: an administrator is waiting for the answer.
#: It is a deadline for the whole call, not a per-chunk read timeout.
TIMEOUT_S: Final = 5.0
#: ``ServerInfo`` is a few hundred bytes; anything past this is not an answer.
MAX_PROOF_BODY_BYTES: Final = 64 * 1024

logger = logging.getLogger(__name__)


class HttpPublicUrlProbe:
    """The ``PublicUrlProbe`` port, over HTTP."""

    def __init__(
        self, transport: httpx2.AsyncBaseTransport | None = None, timeout_s: float = TIMEOUT_S
    ) -> None:
        self._transport = transport
        self._timeout_s = timeout_s

    async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
        """Ask ``public_url`` for ``server/info`` with the nonce, and read the proof."""
        session = HttpSession(
            public_url,
            headers={VERIFY_NONCE_HEADER: nonce},
            verify_tls=True,
            timeout_s=self._timeout_s,
            transport=self._transport,
        )
        try:
            async with asyncio.timeout(self._timeout_s), session:
                response = await session.get_bounded(SERVER_INFO_PATH, MAX_PROOF_BODY_BYTES)
                return self._read(response)
        except TimeoutError:
            logger.info("the public URL check ran out of time", extra=_log("timeout"))
            return ProofResponse.failed("unreachable")
        except RemoteCallError as failure:
            logger.info("the public URL check did not reach a server", extra=_log(failure.reason))
            return ProofResponse.failed(
                "tls_error" if failure.reason == "tls_error" else "unreachable"
            )

    @staticmethod
    def _read(response: httpx2.Response) -> ProofResponse:
        if response.is_redirect:
            # Deliberate: the address must answer for itself, not point elsewhere.
            logger.info("the public URL answered a redirect", extra=_log("redirected"))
            return ProofResponse.failed("redirected")
        if response.status_code != HTTPStatus.OK:
            logger.info("the public URL did not answer 200", extra=_log("status"))
            return ProofResponse.failed("unexpected_response")
        try:
            proof = as_text(read_mapping(response).get(PROOF_FIELD))
        except RemoteCallError:
            proof = None
        if proof is None:
            logger.info("the public URL answered without a proof", extra=_log("no_proof"))
            return ProofResponse.failed("unexpected_response")
        return ProofResponse(proof=proof)


def _log(reason: str) -> dict[str, str]:
    # The address is never logged: it is what the administrator typed, and the reason
    # is all an operator needs to act on.
    return {"reason": reason}
