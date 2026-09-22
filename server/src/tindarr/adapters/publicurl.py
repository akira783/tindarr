"""Calling this server's own public address, to check ``public_url`` before saving it.

The one outbound call Tindarr makes to an address an administrator typed, so it is the
narrowest client in the code base (the security model, section 7, SSRF):

- **no redirect is ever followed.** A redirect is a failure (``redirected``), not a
  hop: following one would let a typo point the check at a server that happens to
  answer, and hand it the nonce;
- **TLS is always verified**, whatever the media server connector's own setting;
- **five seconds**, once. A hanging address must not hold an administrator's request;
- the body is bounded like every other adapter's, and only ``public_url_proof`` is
  read from it. No response body, status or address ever leaves this module.
"""

import logging
from http import HTTPStatus
from typing import Final

import httpx2

from tindarr.adapters.http import HttpSession, RemoteCallError, as_text, read_mapping
from tindarr.ports.publicurl import PROOF_FIELD, VERIFY_NONCE_HEADER, ProofResponse

#: The path the proof is read from, on the public address.
SERVER_INFO_PATH: Final = "/api/v1/server/info"
#: Shorter than every other outbound call: an administrator is waiting for the answer.
TIMEOUT_S: Final = 5.0

logger = logging.getLogger(__name__)


class HttpPublicUrlProbe:
    """The ``PublicUrlProbe`` port, over HTTP."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
        """Ask ``public_url`` for ``server/info`` with the nonce, and read the proof."""
        session = HttpSession(
            public_url,
            headers={VERIFY_NONCE_HEADER: nonce},
            verify_tls=True,
            timeout_s=TIMEOUT_S,
            transport=self._transport,
        )
        try:
            async with session:
                response = await session.request("GET", SERVER_INFO_PATH)
                return self._read(response)
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
