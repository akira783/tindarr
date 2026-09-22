"""Connecting a phone: the console's half and the app's (docs/auth.md, section 9).

Two routers, because the two halves are authenticated in opposite ways:

- ``/pairings/…`` is **console only** — a web session and, on the unsafe methods, a
  CSRF token. A token stolen from a phone cannot create or approve a pairing;
- ``/auth/pair/…`` is **public**: the phone has no credential yet, that is the point.
  Its three calls are protected by the code (128 bits, hashed, five minutes, single
  use), by PKCE, by the human approval in the console, and by the per-IP limit the
  three of them share.

Every refusal on the public half is the same ``410``, so nothing can be learned by
trying codes; the exceptions are the ``202`` while a phone waits and the ``403`` once
the user has refused it, both of which need a matching verifier first.
"""

import math
from typing import Annotated

from fastapi import APIRouter, Path, Response, status

from tindarr.api.deps import Services
from tindarr.api.security import Context, WebSession
from tindarr.api.v1.models import (
    AuthResultResponse,
    CompletePairingInput,
    NewPairingResponse,
    PairDeviceInput,
    PairingCodeInput,
    PairingPreviewResponse,
    PairingRequestedResponse,
    PairingResponse,
)

router = APIRouter(prefix="/pairings", tags=["pairing", "console"])
app_router = APIRouter(prefix="/auth/pair", tags=["auth", "pairing"])

PairingId = Annotated[str, Path(description="The pairing to act on.", max_length=64)]


# --- the console ---------------------------------------------------------------------


@router.post(
    "",
    operation_id="createPairing",
    summary="Create a code to connect a phone (any signed-in user)",
    status_code=status.HTTP_201_CREATED,
)
async def create_pairing(services: Services, session: WebSession) -> NewPairingResponse:
    """Create a pairing bound to the caller and return its code and link, once."""
    public_url = (await services.settings.get("public_url")).value
    created = await services.pairings.create(
        session.signed_in_user, public_url if isinstance(public_url, str) else None
    )
    return NewPairingResponse.created(created, services.clock.now())


@router.get(
    "/{pairing_id}",
    operation_id="getPairing",
    summary="Status of one of the caller's pairings",
)
async def get_pairing(
    pairing_id: PairingId, services: Services, session: WebSession
) -> PairingResponse:
    """Tell the console where this pairing stands, and how long to wait before asking again."""
    pairing = await services.pairings.owned(pairing_id, session.signed_in_user)
    return PairingResponse.of(pairing, services.clock.now())


@router.post(
    "/{pairing_id}/approve",
    operation_id="approvePairing",
    summary="Approve the phone waiting on this pairing",
)
async def approve_pairing(
    pairing_id: PairingId, services: Services, session: WebSession
) -> PairingResponse:
    """Let the waiting phone collect its tokens at its next poll."""
    pairing = await services.pairings.approve(pairing_id, session.signed_in_user)
    return PairingResponse.of(pairing, services.clock.now())


@router.delete(
    "/{pairing_id}",
    operation_id="revokePairing",
    summary="Cancel or reject a pairing, or revoke the session it opened",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def revoke_pairing(pairing_id: PairingId, services: Services, session: WebSession) -> None:
    """Cancel it, refuse the phone waiting on it, or sign out the phone it connected."""
    await services.pairings.revoke(pairing_id, session.signed_in_user)


# --- the app -------------------------------------------------------------------------


async def _spend_public_call(services: Services, context: Context, *, polled: bool = False) -> None:
    """Count one public pairing call against its per-address limit and the global one.

    ``polled`` picks the completion budget, which is sized for the two-second cadence
    the server itself asks for; preview and pair share the tighter one, because they
    are the calls somebody could grind against a code. Globally nothing is refused,
    only slowed down, so hammering the endpoint cannot keep a household from pairing.
    """
    limit = services.limits.pairing_complete if polled else services.limits.pairing
    limit.hit(context.rate_limit_key)
    await services.limits.pairing_global.admit()
    services.limits.pairing_global.record()


@app_router.post(
    "/preview",
    operation_id="previewPairing",
    summary="Show who a pairing code signs in, without using it",
)
async def preview_pairing(
    payload: PairingCodeInput, services: Services, context: Context
) -> PairingPreviewResponse:
    """Return the server and user names the phone shows before the user confirms."""
    await _spend_public_call(services, context)
    preview = await services.pairings.preview(payload.code, await services.sign_in.server_name())
    return PairingPreviewResponse(
        server_name=preview.server_name,
        user_name=preview.user_name,
        expires_at=preview.expires_at,
    )


@app_router.post(
    "",
    operation_id="pairDevice",
    summary="Ask to pair the app with a code from the console",
    status_code=status.HTTP_202_ACCEPTED,
)
async def pair_device(
    payload: PairDeviceInput, response: Response, services: Services, context: Context
) -> PairingRequestedResponse:
    """Record the phone and its challenge, and hand back the four digits to display."""
    await _spend_public_call(services, context)
    requested = await services.pairings.request(
        payload.code,
        device=payload.device.as_device(),
        code_challenge=payload.code_challenge,
        client_ip=None if context.client is None else str(context.client),
    )
    body = PairingRequestedResponse(
        confirmation_code=requested.confirmation_code, expires_at=requested.expires_at
    )
    response.headers["Retry-After"] = str(math.ceil(body.retry_after_ms / 1000))
    return body


@app_router.post(
    "/complete",
    operation_id="completePairing",
    summary="Get the tokens once the console approved the pairing",
)
async def complete_pairing(
    payload: CompletePairingInput, services: Services, context: Context
) -> AuthResultResponse:
    """Open the phone's session once the user approved it; ``202`` until then."""
    await _spend_public_call(services, context, polled=True)
    completed = await services.pairings.complete(
        payload.code, payload.code_verifier, client_is_private=context.client_is_private
    )
    return AuthResultResponse.of(completed.grant.tokens, completed.user)
