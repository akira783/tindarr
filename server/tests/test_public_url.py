"""`public_url`: the proof, the checks it refuses, and who may change it (auth.md §10).

Check (a) of the roadmap — "proof checked, redirects not followed, nonce not answered
when not pending" — plus the role and freshness rules of section 7.

The happy path is not faked: the probe calls the running application over
``httpx2.ASGITransport``, so the nonce really travels to ``GET /server/info`` through
the whole middleware stack and the proof really comes back from the key.
"""

import logging
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from fastapi import FastAPI
from starlette.types import ASGIApp

from tests.support import (
    API,
    CONSOLE_HOST,
    TEST_HOST,
    FakeClock,
    FakeInternet,
    build_app,
    console_client,
    console_headers,
    seed_jellyfin,
    set_up_server,
    web_login,
)
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.adapters.publicurl import MAX_PROOF_BODY_BYTES, HttpPublicUrlProbe
from tindarr.auth.access import REAUTH_WINDOW
from tindarr.auth.publicurl import (
    MAX_PENDING_NONCES,
    NONCE_LIFETIME,
    PublicUrlVerifier,
    proof_for,
)
from tindarr.core.errors import ProblemError
from tindarr.jobs.publicurl import public_url_check_job
from tindarr.ports.publicurl import PROOF_FIELD, VERIFY_NONCE_HEADER, ProofResponse

SETTINGS = f"{API}/admin/settings"
PUBLIC_HOST = "tindarr.example.com"
PUBLIC_URL = f"https://{PUBLIC_HOST}"
KEY = b"k" * 32


# --- the verifier -----------------------------------------------------------------------


class StubProbe:
    """A probe that answers what the test tells it to, and remembers what it was asked."""

    def __init__(
        self, answer: ProofResponse | None = None, key: bytes | None = None, host: str = PUBLIC_HOST
    ) -> None:
        self.answer = answer
        self.key = key
        self.host = host
        self.calls: list[tuple[str, str]] = []

    async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
        """Record the call and answer as configured."""
        self.calls.append((public_url, nonce))
        if self.key is not None:
            return ProofResponse(proof=proof_for(self.key, nonce, self.host))
        return self.answer or ProofResponse.failed("unreachable")


@pytest.mark.anyio
async def test_a_matching_proof_passes(clock: FakeClock) -> None:
    probe = StubProbe(key=KEY)
    await PublicUrlVerifier(KEY, probe, clock).verify(PUBLIC_URL, PUBLIC_HOST)
    assert probe.calls[0][0] == PUBLIC_URL


@pytest.mark.anyio
async def test_a_proof_meant_for_another_host_does_not_pass(clock: FakeClock) -> None:
    # A relay that asked this very server for the proof under its own name (or any
    # other) gets one that is worthless for the candidate host.
    probe = StubProbe(key=KEY, host="192.168.1.20")
    verifier = PublicUrlVerifier(KEY, probe, clock)
    with pytest.raises(ProblemError) as refused:
        await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert refused.value.extensions["reason"] == "proof_mismatch"


@pytest.mark.anyio
async def test_another_servers_answer_is_refused(clock: FakeClock) -> None:
    # Another Tindarr has neither the nonce nor the key, so its proof cannot match.
    verifier = PublicUrlVerifier(KEY, StubProbe(key=b"someone else's key" + b"0" * 14), clock)
    with pytest.raises(ProblemError) as refused:
        await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert refused.value.code == "public_url_unverified"
    assert refused.value.extensions["reason"] == "proof_mismatch"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure", ["unreachable", "tls_error", "redirected", "unexpected_response"]
)
async def test_every_coarse_reason_reaches_the_caller(clock: FakeClock, failure: Any) -> None:
    verifier = PublicUrlVerifier(KEY, StubProbe(ProofResponse.failed(failure)), clock)
    with pytest.raises(ProblemError) as refused:
        await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert refused.value.extensions["reason"] == failure


@pytest.mark.anyio
async def test_a_nonce_is_answered_once_and_never_again(clock: FakeClock) -> None:
    seen: list[str] = []

    class Peeking:
        async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
            seen.append(nonce)
            answered = verifier.proof(nonce, PUBLIC_HOST)
            assert answered == proof_for(KEY, nonce, PUBLIC_HOST)
            # One nonce, one answer: a second caller holding it gets nothing.
            assert verifier.proof(nonce, PUBLIC_HOST) is None
            return ProofResponse(proof=answered)

    verifier = PublicUrlVerifier(KEY, Peeking(), clock)
    await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert verifier.proof(seen[0], PUBLIC_HOST) is None


@pytest.mark.anyio
async def test_a_nonce_nobody_asked_for_is_never_answered(clock: FakeClock) -> None:
    verifier = PublicUrlVerifier(KEY, StubProbe(), clock)
    assert verifier.proof("made-up", PUBLIC_HOST) is None
    assert verifier.proof(None, PUBLIC_HOST) is None
    assert verifier.proof("", PUBLIC_HOST) is None


@pytest.mark.anyio
async def test_a_nonce_is_forgotten_after_a_minute(clock: FakeClock) -> None:
    held: list[str] = []

    class Slow:
        async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
            held.append(nonce)
            clock.advance(NONCE_LIFETIME * 2)
            return ProofResponse(proof=proof_for(KEY, nonce, PUBLIC_HOST))

    verifier = PublicUrlVerifier(KEY, Slow(), clock)
    await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert verifier.proof(held[0], PUBLIC_HOST) is None


@pytest.mark.anyio
async def test_a_failing_probe_still_drops_its_nonce(clock: FakeClock) -> None:
    held: list[str] = []

    class Exploding:
        async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
            held.append(nonce)
            msg = "boom"
            raise RuntimeError(msg)

    verifier = PublicUrlVerifier(KEY, Exploding(), clock)
    with pytest.raises(RuntimeError):
        await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert verifier.proof(held[0], PUBLIC_HOST) is None


@pytest.mark.anyio
async def test_too_many_checks_at_once_are_refused_rather_than_silently_dropped(
    clock: FakeClock,
) -> None:
    # Evicting the oldest nonce would make a check that is still running fail with a
    # mismatch it cannot explain; the newcomer is refused instead.
    depth = 0

    class Nesting:
        async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
            nonlocal depth
            depth += 1
            if depth <= MAX_PENDING_NONCES:
                await verifier.verify(public_url, PUBLIC_HOST)
            return ProofResponse(proof=proof_for(KEY, nonce, PUBLIC_HOST))

    verifier = PublicUrlVerifier(KEY, Nesting(), clock)
    with pytest.raises(ProblemError) as refused:
        await verifier.verify(PUBLIC_URL, PUBLIC_HOST)
    assert refused.value.code == "rate_limited"


# --- the adapter ---------------------------------------------------------------------------


def probe_over(handler: Callable[[httpx2.Request], httpx2.Response]) -> HttpPublicUrlProbe:
    return HttpPublicUrlProbe(httpx2.MockTransport(handler))


@pytest.mark.anyio
async def test_the_probe_reads_the_proof_and_sends_the_nonce() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["nonce"] = request.headers[VERIFY_NONCE_HEADER]
        seen["path"] = request.url.path
        return httpx2.Response(200, json={PROOF_FIELD: "the-proof"})

    answer = await probe_over(handler).fetch_proof(PUBLIC_URL, "n1")
    assert answer.proof == "the-proof"
    assert seen == {"nonce": "n1", "path": "/api/v1/server/info"}


@pytest.mark.anyio
async def test_the_probe_never_follows_a_redirect() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "https://elsewhere.example/api"})

    assert await probe_over(handler).fetch_proof(PUBLIC_URL, "n1") == ProofResponse.failed(
        "redirected"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(404),
        httpx2.Response(200, json={"name": "something else"}),
        httpx2.Response(200, content=b"not json"),
        httpx2.Response(200, json=["not an object"]),
    ],
)
async def test_the_probe_reports_an_unexpected_answer(response: httpx2.Response) -> None:
    answer = await probe_over(lambda _request: response).fetch_proof(PUBLIC_URL, "n1")
    assert answer == ProofResponse.failed("unexpected_response")


@pytest.mark.anyio
async def test_an_unreachable_address_is_unreachable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "no route"
        raise httpx2.ConnectError(msg)

    assert await probe_over(handler).fetch_proof(PUBLIC_URL, "n1") == ProofResponse.failed(
        "unreachable"
    )


@pytest.mark.anyio
async def test_a_body_that_does_not_stop_is_abandoned() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"x" * (MAX_PROOF_BODY_BYTES + 1))

    assert await probe_over(handler).fetch_proof(PUBLIC_URL, "n1") == ProofResponse.failed(
        "unreachable"
    )


@pytest.mark.anyio
async def test_an_address_that_never_finishes_answering_gives_up() -> None:
    # The read timeout is per chunk, so a trickle would otherwise hold the request —
    # and the administrator — forever. The deadline covers the whole call.
    async def handler(request: httpx2.Request) -> httpx2.Response:
        await anyio.sleep(10)
        return httpx2.Response(200, json={PROOF_FIELD: "too late"})

    probe = HttpPublicUrlProbe(httpx2.MockTransport(handler), timeout_s=0.05)
    with anyio.fail_after(5):
        assert await probe.fetch_proof(PUBLIC_URL, "n1") == ProofResponse.failed("unreachable")


@pytest.mark.anyio
async def test_a_tls_failure_is_reported_as_one() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        msg = "handshake"
        raise httpx2.ConnectError(msg) from ssl.SSLError("certificate verify failed")

    assert await probe_over(handler).fetch_proof(PUBLIC_URL, "n1") == ProofResponse.failed(
        "tls_error"
    )


# --- through the API ------------------------------------------------------------------------


class SelfProbe:
    """The real probe, pointed at the running application itself.

    This is what a reverse proxy in front of the same instance looks like: the nonce
    goes out over HTTP and comes back as a proof only the running server can compute.
    """

    def __init__(self) -> None:
        self.app: ASGIApp | None = None
        self.calls: list[tuple[str, str]] = []

    async def fetch_proof(self, public_url: str, nonce: str) -> ProofResponse:
        """Call the application as the public address would answer."""
        self.calls.append((public_url, nonce))
        assert self.app is not None, "set .app before the check runs"
        probe = HttpPublicUrlProbe(httpx2.ASGITransport(app=self.app))
        return await probe.fetch_proof(public_url, nonce)


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


def app_with(
    data_dir: Path, clock: FakeClock, internet: FakeInternet, probe: Any, **overrides: Any
) -> FastAPI:
    """An application whose public-URL probe is the test's.

    ``PUBLIC_HOST`` is an allowed host, as it is in production: the console is opened
    at the address it is about to declare, or the operator listed it. The proof is
    bound to that host, so an address this server does not answer to cannot pass.
    """
    values: dict[str, Any] = {"allowed_hosts": f"{CONSOLE_HOST},{TEST_HOST},{PUBLIC_HOST}"}
    return build_app(
        data_dir, clock=clock, internet=internet, public_url_probe=probe, **(values | overrides)
    )


def test_a_verified_address_is_saved_and_becomes_an_allowed_host(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    probe = SelfProbe()
    app = app_with(data_dir, clock, internet, probe)
    probe.app = app
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(
            SETTINGS, json={"public_url": PUBLIC_URL}, headers=console_headers(csrf)
        )
        assert response.status_code == 200, response.text
        assert_matches_contract(SETTINGS, "patch", response)
        assert response.json()["public_url"] == PUBLIC_URL
        # The nonce is not answered any more, and never was to anyone else.
        nonce = probe.calls[0][1]
        served = client.get(f"{API}/server/info", headers={VERIFY_NONCE_HEADER: nonce})
        assert PROOF_FIELD not in served.json()
        assert PROOF_FIELD not in client.get(f"{API}/server/info").json()
        assert "pairing" in client.get(f"{API}/server/info").json()["auth_methods"]
    # The console can now be reached at that name too (docs/auth.md, section 1).
    with console_client(app, host=PUBLIC_HOST) as public:
        assert public.get(f"{API}/server/info").status_code == 200


def test_a_trailing_slash_is_normalised_away(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    probe = SelfProbe()
    app = app_with(data_dir, clock, internet, probe)
    probe.app = app
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(
            SETTINGS, json={"public_url": f"{PUBLIC_URL}/"}, headers=console_headers(csrf)
        )
        assert response.json()["public_url"] == PUBLIC_URL


def test_an_unverified_address_is_not_saved(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    app = app_with(data_dir, clock, internet, StubProbe(ProofResponse.failed("redirected")))
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(
            SETTINGS, json={"public_url": PUBLIC_URL}, headers=console_headers(csrf)
        )
        assert_is_problem(response, 409, "public_url_unverified")
        assert response.json()["reason"] == "redirected"
        assert client.get(SETTINGS).json()["public_url"] is None
        assert "pairing" not in client.get(f"{API}/server/info").json()["auth_methods"]


def test_a_host_this_server_does_not_answer_to_is_refused_before_any_call(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    probe = StubProbe(key=KEY)
    app = app_with(data_dir, clock, internet, probe)
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(
            SETTINGS, json={"public_url": "https://somewhere.else"}, headers=console_headers(csrf)
        )
        assert_is_problem(response, 409, "public_url_unverified")
        assert response.json()["reason"] == "host_not_allowed"
        assert "TINDARR_ALLOWED_HOSTS" in response.json()["detail"]
    # Nothing was called: the proof is bound to that host, so no answer could pass.
    assert probe.calls == []


@pytest.mark.parametrize(
    "value",
    [
        "http://tindarr.example.com",
        "https://tindarr.example.com/console",
        "https://user:pw@tindarr.example.com",
        "ftp://tindarr.example.com",
        "tindarr.example.com",
    ],
)
def test_an_address_that_is_not_an_origin_is_refused_before_any_call(
    data_dir: Path, clock: FakeClock, internet: FakeInternet, value: str
) -> None:
    probe = StubProbe()
    app = app_with(data_dir, clock, internet, probe)
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(SETTINGS, json={"public_url": value}, headers=console_headers(csrf))
        assert_is_problem(response, 400, "validation_error")
        assert value not in response.text
    assert probe.calls == []


def test_clearing_the_address_needs_no_check(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    probe = SelfProbe()
    app = app_with(data_dir, clock, internet, probe)
    probe.app = app
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        client.patch(SETTINGS, json={"public_url": PUBLIC_URL}, headers=console_headers(csrf))
        cleared = client.patch(SETTINGS, json={"public_url": None}, headers=console_headers(csrf))
        assert cleared.status_code == 200
        assert cleared.json()["public_url"] is None
        assert len(probe.calls) == 1
        assert "pairing" not in client.get(f"{API}/server/info").json()["auth_methods"]


def test_a_locked_address_cannot_be_changed_from_the_console(
    data_dir: Path, clock: FakeClock, internet: FakeInternet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TINDARR_PUBLIC_URL", PUBLIC_URL)
    probe = StubProbe()
    app = app_with(data_dir, clock, internet, probe)
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        response = client.patch(
            SETTINGS, json={"public_url": "https://other.example"}, headers=console_headers(csrf)
        )
        assert_is_problem(response, 409, "setting_locked")
        assert client.get(SETTINGS).json()["locked_fields"] == ["public_url"]
    # Nothing was called: a locked value is refused before any address is contacted.
    assert probe.calls == []


# --- the one-shot check of an environment value ----------------------------------------------


@pytest.mark.anyio
async def test_a_failing_environment_address_is_only_logged(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    job = public_url_check_job(
        PublicUrlVerifier(KEY, StubProbe(ProofResponse.failed("tls_error")), clock),
        PUBLIC_URL,
        PUBLIC_HOST,
    )
    with caplog.at_level(logging.WARNING, logger="tindarr.jobs.publicurl"):
        await job.run_once()
    assert "TINDARR_PUBLIC_URL" in caplog.text
    assert caplog.records[-1].__dict__["reason"] == "tls_error"


@pytest.mark.anyio
async def test_a_working_environment_address_says_so(
    clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    job = public_url_check_job(
        PublicUrlVerifier(KEY, StubProbe(key=KEY), clock), PUBLIC_URL, PUBLIC_HOST
    )
    with caplog.at_level(logging.INFO, logger="tindarr.jobs.publicurl"):
        await job.run_once()
    assert "answers as this server" in caplog.text


def test_the_environment_check_is_only_scheduled_for_a_locked_address(
    data_dir: Path, clock: FakeClock, internet: FakeInternet, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_with(data_dir, clock, internet, StubProbe())
    with console_client(app):
        assert "public_url_check" not in _job_names(app)

    monkeypatch.setenv("TINDARR_PUBLIC_URL", PUBLIC_URL)
    locked = app_with(data_dir, clock, internet, StubProbe())
    with console_client(locked):
        assert "public_url_check" in _job_names(locked)


def _job_names(app: FastAPI) -> set[str]:
    jobs: Any = app.state.jobs
    return {job.name for job in jobs}


# --- who may change it -------------------------------------------------------------------


def test_a_promoted_admin_cannot_change_the_address(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    # Repointing where the phones connect is a media server administrator's decision;
    # a promotion inside Tindarr is not enough (docs/auth.md, section 7).
    probe = StubProbe(key=KEY)
    app = app_with(data_dir, clock, internet, probe)
    with console_client(app) as admin:
        set_up_server(admin, app)
    with console_client(app) as user:
        assert web_login(user, "robin", "another one").status_code == 200
    with console_client(app) as admin:
        signed_in = web_login(admin, "alex", "correct horse")
        assert signed_in.status_code == 200
        robin = next(
            row for row in admin.get(f"{API}/admin/users").json()["users"] if row["name"] == "robin"
        )
        promoted = admin.patch(
            f"{API}/admin/users/{robin['id']}",
            json={"role": "admin"},
            headers=console_headers(signed_in.json()["csrf_token"]),
        )
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "admin"
    with console_client(app) as user:
        signed_in = web_login(user, "robin", "another one")
        assert signed_in.json()["user"]["role"] == "admin"
        response = user.patch(
            SETTINGS,
            json={"public_url": PUBLIC_URL},
            headers=console_headers(signed_in.json()["csrf_token"]),
        )
        assert_is_problem(response, 403, "media_server_admin_required")
    assert probe.calls == []


def test_a_stale_reauthentication_is_refused(
    data_dir: Path, clock: FakeClock, internet: FakeInternet
) -> None:
    probe = StubProbe(key=KEY)
    app = app_with(data_dir, clock, internet, probe)
    with console_client(app) as client:
        csrf = set_up_server(client, app)
        clock.advance(REAUTH_WINDOW * 2)
        response = client.patch(
            SETTINGS, json={"public_url": PUBLIC_URL}, headers=console_headers(csrf)
        )
        assert_is_problem(response, 403, "reauth_required")
    assert probe.calls == []
