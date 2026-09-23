"""The handle registry itself: bindings, purposes, expiry, caps and the sweep."""

from datetime import timedelta

import pytest

from tests.support import FakeClock
from tindarr.auth.handles import (
    MAX_LIFETIME,
    Binding,
    HandleRegistry,
    pkce_challenge,
)
from tindarr.auth.ratelimit import Limit, SlidingWindow
from tindarr.core.errors import PendingError, ProblemError, RateLimitedError

VERIFIER = "v" * 43
CLIENT = "192.168.1.20"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry(clock: FakeClock) -> HandleRegistry:
    limit = SlidingWindow(Limit("handle_creation", 10, timedelta(minutes=15)), clock)
    return HandleRegistry(clock, limit)


def start(registry: HandleRegistry, **fields: object) -> str:
    binding = fields.pop("binding", Binding.pkce(pkce_challenge(VERIFIER)))
    assert isinstance(binding, Binding)
    kind = fields.pop("kind", "quick_connect")
    purpose = fields.pop("purpose", "sign_in")
    assert isinstance(kind, str)
    assert isinstance(purpose, str)
    return registry.create(
        kind,  # type: ignore[arg-type]
        purpose,  # type: ignore[arg-type]
        binding,
        client_key=str(fields.pop("client_key", CLIENT)),
        client_is_private=bool(fields.pop("client_is_private", False)),
        secret="qc-secret",
    ).id


def test_a_verifier_rebuilds_the_challenge_it_was_bound_to() -> None:
    assert Binding.verifier(VERIFIER) == Binding.pkce(pkce_challenge(VERIFIER))
    assert Binding.verifier("other" * 9) != Binding.pkce(pkce_challenge(VERIFIER))


def test_a_cookie_binding_keeps_only_its_hash() -> None:
    binding = Binding.cookie("the-cookie")
    assert "the-cookie" not in binding.value
    assert binding.matches(Binding.cookie("the-cookie"))
    assert not binding.matches(Binding.cookie("another"))


def test_bindings_of_different_kinds_never_match() -> None:
    assert not Binding.session("x").matches(Binding("preauth", "x"))


def test_a_handle_is_returned_for_the_right_purpose_and_binding(
    registry: HandleRegistry,
) -> None:
    handle_id = start(registry)
    found = registry.use(handle_id, "quick_connect", "sign_in", Binding.verifier(VERIFIER))
    assert found.secret == "qc-secret"


@pytest.mark.parametrize(
    ("kind", "purpose", "binding"),
    [
        ("plex_pin", "sign_in", Binding.verifier(VERIFIER)),
        ("quick_connect", "reauth", Binding.verifier(VERIFIER)),
        ("quick_connect", "sign_in", Binding.verifier("w" * 43)),
        ("quick_connect", "sign_in", Binding.session("nope")),
    ],
)
def test_anything_wrong_answers_the_same_way(
    registry: HandleRegistry, kind: str, purpose: str, binding: Binding
) -> None:
    handle_id = start(registry)
    with pytest.raises(ProblemError) as caught:
        registry.use(handle_id, kind, purpose, binding)  # type: ignore[arg-type]
    assert caught.value.status == 410


def test_an_unknown_handle_answers_like_a_wrong_one(registry: HandleRegistry) -> None:
    with pytest.raises(ProblemError) as caught:
        registry.use("nope", "plex_pin", "sign_in", Binding.session("x"))
    assert (caught.value.status, caught.value.code) == (410, "pin_expired")


def test_a_handle_never_outlives_its_cap(registry: HandleRegistry, clock: FakeClock) -> None:
    far = clock.now() + timedelta(hours=2)
    handle = registry.create(
        "quick_connect", "sign_in", Binding.session("s"), client_key=CLIENT, upstream_expiry=far
    )
    assert handle.expires_at == clock.now() + MAX_LIFETIME["quick_connect"]


def test_a_shorter_upstream_expiry_wins(registry: HandleRegistry, clock: FakeClock) -> None:
    soon = clock.now() + timedelta(minutes=1)
    handle = registry.create(
        "plex_pin", "sign_in", Binding.session("s"), client_key=CLIENT, upstream_expiry=soon
    )
    assert handle.expires_at == soon


def test_an_expired_handle_is_gone(registry: HandleRegistry, clock: FakeClock) -> None:
    handle_id = start(registry)
    clock.advance(MAX_LIFETIME["quick_connect"])
    with pytest.raises(ProblemError):
        registry.use(handle_id, "quick_connect", "sign_in", Binding.verifier(VERIFIER))
    assert registry.outstanding == 0


def test_a_spent_handle_is_gone(registry: HandleRegistry) -> None:
    handle_id = start(registry)
    registry.spend(registry.use(handle_id, "quick_connect", "sign_in", Binding.verifier(VERIFIER)))
    with pytest.raises(ProblemError):
        registry.use(handle_id, "quick_connect", "sign_in", Binding.verifier(VERIFIER))


def test_a_dropped_handle_is_gone(registry: HandleRegistry) -> None:
    handle_id = start(registry)
    registry.drop(handle_id)
    assert registry.outstanding == 0


def test_polling_is_limited_to_once_a_second(registry: HandleRegistry, clock: FakeClock) -> None:
    handle = registry.use(start(registry), "quick_connect", "sign_in", Binding.verifier(VERIFIER))
    registry.throttle(handle)
    with pytest.raises(PendingError) as caught:
        registry.throttle(handle)
    assert caught.value.retry_after_ms == 1000
    clock.advance(1.5)
    registry.throttle(handle)


def test_five_handles_per_client_is_the_limit(registry: HandleRegistry) -> None:
    for _ in range(5):
        start(registry)
    with pytest.raises(RateLimitedError):
        start(registry)
    # Another client is unaffected.
    start(registry, client_key="10.0.0.9")


def test_the_global_cap_is_checked_first(clock: FakeClock) -> None:
    limit = SlidingWindow(Limit("handle_creation", 100, timedelta(minutes=15)), clock)
    registry = HandleRegistry(clock, limit, max_per_client=100, max_total=2)
    start(registry)
    start(registry)
    with pytest.raises(RateLimitedError):
        start(registry)


def test_the_global_cap_takes_the_slot_from_the_greediest_client(clock: FakeClock) -> None:
    """M2: a full table must not be a household-wide lockout.

    On a Plex server the PIN is the only way in, so strangers holding every slot would
    deny every sign-in in the house. The cap still holds — the table never grows — but
    the slot comes from whoever hoards the most.
    """
    limit = SlidingWindow(Limit("handle_creation", 1000, timedelta(minutes=15)), clock)
    registry = HandleRegistry(clock, limit, max_total=10)
    greedy = [start(registry, client_key="203.0.113.7") for _ in range(5)]
    for index in range(5):
        start(registry, client_key=f"198.51.100.{index}")
    assert registry.outstanding == 10

    mine = start(registry, client_key="192.168.1.30", client_is_private=True)

    assert registry.outstanding == 10  # the memory bound is untouched
    registry.use(mine, "quick_connect", "sign_in", Binding.verifier(VERIFIER))
    with pytest.raises(ProblemError):  # the greediest client lost its oldest, only it
        registry.use(greedy[0], "quick_connect", "sign_in", Binding.verifier(VERIFIER))
    registry.use(greedy[1], "quick_connect", "sign_in", Binding.verifier(VERIFIER))


def test_a_stranger_never_takes_the_slot_of_a_client_on_the_local_network(
    clock: FakeClock,
) -> None:
    limit = SlidingWindow(Limit("handle_creation", 1000, timedelta(minutes=15)), clock)
    registry = HandleRegistry(clock, limit, max_total=4)
    household = [
        start(registry, client_key=f"192.168.1.{index}", client_is_private=True)
        for index in range(4)
    ]
    with pytest.raises(RateLimitedError):
        start(registry, client_key="203.0.113.7")
    assert registry.outstanding == 4
    for handle_id in household:
        registry.use(handle_id, "quick_connect", "sign_in", Binding.verifier(VERIFIER))


def test_an_evicted_quick_connect_handle_still_reaches_the_sweep(clock: FakeClock) -> None:
    # It may already hold a Jellyfin session: the sweep is what closes it.
    limit = SlidingWindow(Limit("handle_creation", 1000, timedelta(minutes=15)), clock)
    registry = HandleRegistry(clock, limit, max_total=1)
    evicted = start(registry, client_key="203.0.113.7")
    start(registry, client_key="192.168.1.30", client_is_private=True)
    assert [handle.id for handle in registry.sweep()] == [evicted]


def test_creation_is_limited_over_a_window(registry: HandleRegistry, clock: FakeClock) -> None:
    for _ in range(10):
        handle_id = start(registry)
        registry.drop(handle_id)
    with pytest.raises(RateLimitedError):
        start(registry)


def test_the_sweep_returns_quick_connect_handles_nobody_collected(
    registry: HandleRegistry, clock: FakeClock
) -> None:
    abandoned = start(registry)
    collected = start(registry)
    registry.use(collected, "quick_connect", "sign_in", Binding.verifier(VERIFIER)).collected = True
    pin = registry.create("plex_pin", "sign_in", Binding.session("s"), client_key=CLIENT)
    clock.advance(MAX_LIFETIME["plex_pin"])
    swept = registry.sweep()
    assert [handle.id for handle in swept] == [abandoned]
    assert registry.outstanding == 0
    assert pin.expired(clock.now())


def test_an_expired_handle_still_reaches_the_sweep_after_it_was_pruned(
    registry: HandleRegistry, clock: FakeClock
) -> None:
    abandoned = start(registry)
    clock.advance(MAX_LIFETIME["quick_connect"])
    # Anything touching the registry drops expired handles, so the caps stay right.
    with pytest.raises(ProblemError):
        registry.use(abandoned, "quick_connect", "sign_in", Binding.verifier(VERIFIER))
    assert registry.outstanding == 0
    # It is still handed to the sweep, which is what closes its Jellyfin session.
    assert [handle.id for handle in registry.sweep()] == [abandoned]
    assert registry.sweep() == []


def test_the_sweep_leaves_live_handles_alone(registry: HandleRegistry) -> None:
    start(registry)
    assert registry.sweep() == []
    assert registry.outstanding == 1


def test_an_owner_token_handle_is_peeked_then_consumed(registry: HandleRegistry) -> None:
    handle = registry.create(
        "plex_pin", "owner_token", Binding.session("session-1"), client_key=CLIENT
    )
    assert registry.outstanding == 1
    handle.token = "plex-token"
    return_value = registry.use(handle.id, "plex_pin", "owner_token", Binding.session("session-1"))
    assert return_value.token == "plex-token"


async def test_peeking_an_unapproved_owner_token_says_so(registry: HandleRegistry) -> None:
    handle = registry.create(
        "plex_pin", "owner_token", Binding.session("session-1"), client_key=CLIENT
    )
    with pytest.raises(ProblemError) as caught:
        await registry.peek(handle.id, "session-1")
    assert (caught.value.status, caught.value.code) == (409, "plex_pin_pending")


async def test_an_owner_token_handle_is_bound_to_its_session(registry: HandleRegistry) -> None:
    handle = registry.create(
        "plex_pin", "owner_token", Binding.session("session-1"), client_key=CLIENT
    )
    handle.token = "plex-token"
    assert await registry.peek(handle.id, "session-1") == "plex-token"
    with pytest.raises(ProblemError):
        await registry.peek(handle.id, "session-2")
    await registry.consume(handle.id, "session-1")
    with pytest.raises(ProblemError):
        await registry.peek(handle.id, "session-1")


pytestmark = pytest.mark.anyio
