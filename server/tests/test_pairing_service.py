"""The pairing state machine, driven directly (docs/auth.md, section 9).

``test_pairing_api`` drives the same flow over HTTP; this module goes at the parts an
HTTP test cannot reach comfortably: the races two callers can start on one pairing, and
the state a pairing is in after each of them.
"""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import FakeClock, media_user
from tindarr.auth.handles import pkce_challenge
from tindarr.auth.pairing import (
    MAX_OPEN_PAIRINGS,
    PAIRING_LIFETIME,
    PairingService,
    confirmation_code,
    pairing_link,
)
from tindarr.auth.sessions import SessionService
from tindarr.auth.users import link_user
from tindarr.core.errors import PendingError, ProblemError
from tindarr.storage import pairings as repository
from tindarr.storage import users as user_repository
from tindarr.storage.db import write_transaction
from tindarr.storage.sessions import Device
from tindarr.storage.users import User

pytestmark = pytest.mark.anyio

PUBLIC_URL = "https://tindarr.example.com"
VERIFIER = "v" * 43
PHONE = Device("Pixel 9", "android", "0.1.0")


@pytest.fixture
def pairings(engine: AsyncEngine, clock: FakeClock, sessions: SessionService) -> PairingService:
    return PairingService(engine, clock, sessions)


@pytest.fixture
async def owner(engine: AsyncEngine, clock: FakeClock) -> User:
    async with write_transaction(engine) as connection:
        return await link_user(connection, media_user(name="Ada"), clock.now())


def challenge() -> str:
    """The PKCE S256 challenge of the verifier, as the app computes it."""
    return pkce_challenge(VERIFIER)


async def requested(pairings: PairingService, owner: User) -> tuple[str, str]:
    """Create a pairing and have a phone ask on it; return its id and its code."""
    created = await pairings.create(owner, PUBLIC_URL)
    await pairings.request(
        created.code, device=PHONE, code_challenge=challenge(), client_ip="192.168.1.31"
    )
    return created.pairing.id, created.code


# --- creating --------------------------------------------------------------------------


async def test_creating_needs_a_public_url(pairings: PairingService, owner: User) -> None:
    with pytest.raises(ProblemError) as refused:
        await pairings.create(owner, None)
    assert (refused.value.status, refused.value.code) == (409, "public_url_not_set")


async def test_the_code_is_stored_hashed_and_shown_once(
    pairings: PairingService, owner: User, engine: AsyncEngine
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    async with engine.connect() as connection:
        stored = await repository.get(connection, created.pairing.id)
    assert stored is not None
    assert created.code not in stored.code_hash
    assert stored.code_hash != created.code
    assert created.link == pairing_link(PUBLIC_URL, created.code)


async def test_the_per_user_cap_says_how_long_to_wait(
    pairings: PairingService, owner: User, clock: FakeClock
) -> None:
    for _ in range(MAX_OPEN_PAIRINGS):
        await pairings.create(owner, PUBLIC_URL)
    with pytest.raises(ProblemError) as refused:
        await pairings.create(owner, PUBLIC_URL)
    assert (refused.value.status, refused.value.code) == (429, "rate_limited")
    assert refused.value.extensions["retry_after_ms"] == int(
        PAIRING_LIFETIME.total_seconds() * 1000
    )
    # A pairing that finished frees a slot at once, without waiting for expiry.
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    await pairings.create(owner, PUBLIC_URL)


# --- the phone's three calls ---------------------------------------------------------------


async def test_the_preview_says_who_would_be_signed_in(
    pairings: PairingService, owner: User
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    preview = await pairings.preview(created.code, "Chez nous")
    assert (preview.server_name, preview.user_name) == ("Chez nous", "Ada")
    assert preview.expires_at == created.pairing.expires_at


@pytest.mark.parametrize("state", ["awaiting_approval", "approved", "completed", "revoked"])
async def test_the_preview_only_answers_a_pending_code(
    pairings: PairingService, owner: User, engine: AsyncEngine, state: str
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    async with write_transaction(engine) as connection:
        await repository.transition(
            connection,
            created.pairing.id,
            expected=("pending",),
            to=state,  # pyright: ignore[reportArgumentType] - the test walks every state
            now=created.pairing.created_at,
        )
    with pytest.raises(ProblemError) as refused:
        await pairings.preview(created.code, "Chez nous")
    assert (refused.value.status, refused.value.code) == (410, "pairing_expired")


async def test_the_request_records_the_phone_and_derives_the_four_digits(
    pairings: PairingService, owner: User, engine: AsyncEngine
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    answer = await pairings.request(
        created.code, device=PHONE, code_challenge=challenge(), client_ip="192.168.1.31"
    )
    assert answer.confirmation_code == confirmation_code(challenge())
    async with engine.connect() as connection:
        stored = await repository.get(connection, created.pairing.id)
    assert stored is not None
    assert stored.status == "awaiting_approval"
    assert stored.device == PHONE
    assert stored.requested_from == "192.168.1.31"
    assert stored.code_challenge == challenge()


async def test_a_second_phone_on_the_same_code_loses(pairings: PairingService, owner: User) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    await pairings.request(
        created.code, device=PHONE, code_challenge=challenge(), client_ip="192.168.1.31"
    )
    with pytest.raises(ProblemError) as refused:
        await pairings.request(
            created.code,
            device=Device("Other phone", "ios", "0.1.0"),
            code_challenge=challenge(),
            client_ip="203.0.113.7",
        )
    assert refused.value.code == "pairing_expired"


async def test_completing_waits_for_the_approval(pairings: PairingService, owner: User) -> None:
    _, code = await requested(pairings, owner)
    with pytest.raises(PendingError) as waiting:
        await pairings.complete(code, VERIFIER, client_is_private=True)
    assert waiting.value.retry_after_ms == 2000


async def test_completing_opens_the_session_and_records_it(
    pairings: PairingService, owner: User, engine: AsyncEngine
) -> None:
    pairing_id, code = await requested(pairings, owner)
    await pairings.approve(pairing_id, owner)
    completed = await pairings.complete(code, VERIFIER, client_is_private=True)
    assert completed.user.id == owner.id
    assert completed.grant.session.device == PHONE
    async with engine.connect() as connection:
        stored = await repository.get(connection, pairing_id)
    assert stored is not None
    assert stored.status == "completed"
    assert stored.session_id == completed.grant.session.id
    # And the code is spent.
    with pytest.raises(ProblemError) as refused:
        await pairings.complete(code, VERIFIER, client_is_private=True)
    assert refused.value.code == "pairing_expired"


async def test_a_wrong_verifier_is_refused_before_the_state_is_revealed(
    pairings: PairingService, owner: User
) -> None:
    pairing_id, code = await requested(pairings, owner)
    await pairings.revoke(pairing_id, owner)
    # With the right verifier the app learns it was rejected...
    with pytest.raises(ProblemError) as rejected:
        await pairings.complete(code, VERIFIER, client_is_private=True)
    assert (rejected.value.status, rejected.value.code) == (403, "pairing_rejected")
    # ...without it, it learns nothing at all.
    with pytest.raises(ProblemError) as refused:
        await pairings.complete(code, "w" * 43, client_is_private=True)
    assert (refused.value.status, refused.value.code) == (410, "pairing_expired")


async def test_a_code_cancelled_before_any_phone_asked_is_just_gone(
    pairings: PairingService, owner: User
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    await pairings.revoke(created.pairing.id, owner)
    with pytest.raises(ProblemError) as refused:
        await pairings.complete(created.code, VERIFIER, client_is_private=True)
    assert refused.value.code == "pairing_expired"


async def test_an_expired_pairing_gives_nothing(
    pairings: PairingService, owner: User, clock: FakeClock
) -> None:
    pairing_id, code = await requested(pairings, owner)
    await pairings.approve(pairing_id, owner)
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    for call in (
        lambda: pairings.preview(code, "Chez nous"),
        lambda: pairings.request(code, device=PHONE, code_challenge=challenge(), client_ip=None),
        lambda: pairings.complete(code, VERIFIER, client_is_private=True),
    ):
        with pytest.raises(ProblemError) as refused:
            await call()
        assert refused.value.code == "pairing_expired"


async def test_an_unknown_code_answers_like_every_other_failure(
    pairings: PairingService,
) -> None:
    with pytest.raises(ProblemError) as refused:
        await pairings.complete("z" * 22, VERIFIER, client_is_private=True)
    assert (refused.value.status, refused.value.code) == (410, "pairing_expired")


# --- the checks completion still applies ------------------------------------------------------


async def test_a_user_disabled_since_the_approval_gets_no_tokens(
    pairings: PairingService, owner: User, engine: AsyncEngine
) -> None:
    pairing_id, code = await requested(pairings, owner)
    await pairings.approve(pairing_id, owner)
    async with write_transaction(engine) as connection:
        await user_repository.update_fields(
            connection, owner.id, enabled=False, disabled_reason="admin"
        )
    with pytest.raises(ProblemError) as refused:
        await pairings.complete(code, VERIFIER, client_is_private=True)
    assert (refused.value.status, refused.value.code) == (403, "account_disabled")


async def test_the_remote_access_rule_applies_to_the_phone(
    pairings: PairingService, owner: User, engine: AsyncEngine
) -> None:
    pairing_id, code = await requested(pairings, owner)
    await pairings.approve(pairing_id, owner)
    async with write_transaction(engine) as connection:
        await user_repository.update_fields(connection, owner.id, remote_access=False)
    with pytest.raises(ProblemError) as refused:
        await pairings.complete(code, VERIFIER, client_is_private=False)
    assert (refused.value.status, refused.value.code) == (403, "remote_access_denied")
    # From the local network, the same code still works.
    assert (await pairings.complete(code, VERIFIER, client_is_private=True)).user.id == owner.id


# --- the console's side ------------------------------------------------------------------------


async def test_another_users_pairing_is_not_found(
    pairings: PairingService, owner: User, engine: AsyncEngine, clock: FakeClock
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    async with write_transaction(engine) as connection:
        other = await link_user(connection, media_user(user_id="b" * 32, name="Bo"), clock.now())
    for call in (
        lambda: pairings.owned(created.pairing.id, other),
        lambda: pairings.approve(created.pairing.id, other),
        lambda: pairings.revoke(created.pairing.id, other),
    ):
        with pytest.raises(ProblemError) as refused:
            await call()
        assert (refused.value.status, refused.value.code) == (404, "not_found")


async def test_approving_twice_loses_the_second_time(pairings: PairingService, owner: User) -> None:
    pairing_id, _ = await requested(pairings, owner)
    assert (await pairings.approve(pairing_id, owner)).status == "approved"
    with pytest.raises(ProblemError) as refused:
        await pairings.approve(pairing_id, owner)
    assert (refused.value.status, refused.value.code) == (409, "pairing_not_awaiting_approval")


async def test_approving_an_expired_pairing_is_refused(
    pairings: PairingService, owner: User, clock: FakeClock
) -> None:
    pairing_id, _ = await requested(pairings, owner)
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    with pytest.raises(ProblemError) as refused:
        await pairings.approve(pairing_id, owner)
    assert (refused.value.status, refused.value.code) == (410, "pairing_expired")


async def test_revoking_a_completed_pairing_signs_the_phone_out(
    pairings: PairingService, owner: User, engine: AsyncEngine, sessions: SessionService
) -> None:
    pairing_id, code = await requested(pairings, owner)
    await pairings.approve(pairing_id, owner)
    completed = await pairings.complete(code, VERIFIER, client_is_private=True)
    await pairings.revoke(pairing_id, owner)
    with pytest.raises(ProblemError):
        await sessions.authenticate_access_token(completed.grant.tokens.access_token)


async def test_revoking_an_expired_pairing_still_tidies_it(
    pairings: PairingService, owner: User, clock: FakeClock, engine: AsyncEngine
) -> None:
    created = await pairings.create(owner, PUBLIC_URL)
    clock.advance(PAIRING_LIFETIME + timedelta(seconds=1))
    await pairings.revoke(created.pairing.id, owner)
    async with engine.connect() as connection:
        stored = await repository.get(connection, created.pairing.id)
    assert stored is not None
    assert stored.status == "revoked"


async def test_the_confirmation_code_is_four_digits_of_the_challenge() -> None:
    assert confirmation_code(challenge()) == confirmation_code(challenge())
    assert len(confirmation_code(challenge())) == 4
    assert confirmation_code(challenge()).isdigit()
    assert confirmation_code("a" * 43) != confirmation_code("b" * 43)
