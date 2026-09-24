"""What the deck does when a generation fails, finds nothing, or nobody asked for it.

The happy path is in ``test_swipe_api``. This is the other half: the decisions the deck
makes when there is no batch to hand over, and the two schedules that run without anybody
polling. Each of them is a way the engine could strand somebody or spend twice, so each
is checked rather than reasoned about:

- a failed generation is reported **once** and then leaves the deck free to try again;
- a batch that came back empty does not start another one, until a vote changes the pool;
- two polls arriving together buy **one** batch;
- a job a restart left behind releases its user's slot instead of holding it for ever;
- the warm-up pays for the people who are here and nobody else.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    API,
    FakeClock,
    FakeInternet,
    build_app,
    console_client,
    console_headers,
    run,
    seed_jellyfin,
    services_of,
    set_up_server,
)
from tests.support.ai import OPENAI_KEY
from tests.support.metadata import TMDB_API_KEY, movie_result
from tests.support.outside import FakeOutside
from tests.test_contract import assert_is_problem
from tindarr.jobs.swipe import close_abandoned_jobs
from tindarr.storage import batches as batch_repository
from tindarr.storage import jobs as job_repository
from tindarr.storage.batches import CARD_RETENTION
from tindarr.storage.db import write_transaction

DECK = f"{API}/swipe/deck"
POOL_IDS = tuple(range(201, 221))


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def outside() -> FakeOutside:
    services = FakeOutside()
    services.tmdb.discovered = [
        movie_result(tmdb_id, f"Candidate {tmdb_id}", vote_count=900) for tmdb_id in POOL_IDS
    ]
    for tmdb_id in POOL_IDS:
        services.tmdb.add_details(
            "movie",
            tmdb_id,
            {"id": tmdb_id, "title": f"Candidate {tmdb_id}", "release_date": "2016-01-01"},
        )
    services.openai.answers = [
        json.dumps(
            {
                "cards": [
                    {"id": tmdb_id, "media_type": "movie", "rationale": "x", "pick_type": "safe"}
                    for tmdb_id in POOL_IDS[:10]
                ]
            }
        )
    ]
    return services


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet, outside: FakeOutside) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet, outside=outside)


def configure(client: TestClient, app: FastAPI) -> str:
    """Set the server up with TMDb and an AI provider; return the CSRF token."""
    csrf = set_up_server(client, app)
    for body in (
        {"connector": "tmdb", "api_key": TMDB_API_KEY},
        {"connector": "llm", "provider": "openai", "api_key": OPENAI_KEY, "model": "model-a"},
    ):
        response = client.put(
            f"{API}/admin/connectors/{body['connector']}",
            json=body,
            headers=console_headers(csrf),
        )
        assert response.status_code == 200, response.text
    return csrf


def drain(client: TestClient, app: FastAPI) -> None:
    run(client, services_of(app).batch_runner.drain)


def exclude_horror(client: TestClient, csrf: str) -> None:
    """Set a content filter, which is what makes TMDb's genre list load-bearing."""
    response = client.patch(
        f"{API}/admin/settings",
        json={"content_filters": {"excluded_genres": ["Horror"]}},
        headers=console_headers(csrf),
    )
    assert response.status_code == 200, response.text


class TestAFailedGeneration:
    def test_it_is_reported_once_and_then_the_deck_tries_again(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            exclude_horror(client, csrf)
            # The genre list is the one TMDb call the pool refuses to guess past: a
            # household that excluded horror has no filter at all until it is read.
            outside.tmdb.offline = True
            assert client.get(DECK).status_code == 202
            drain(client, app)
            first = client.get(DECK)
            outside.tmdb.offline = False
            second = client.get(DECK)
        assert_is_problem(first, 503, "metadata_unreachable")
        assert second.status_code == 202, second.text

    def test_it_spends_the_generation_and_counts_the_failure(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            exclude_horror(client, csrf)
            outside.tmdb.offline = True
            client.get(DECK)
            drain(client, app)
            client.get(DECK)
            usage = client.get(f"{API}/admin/usage").json()["days"][0]
            status = client.get(f"{API}/swipe/status").json()
        assert usage["generations"] == 1
        assert usage["failures"] == 1
        assert status["generations_left_today"] == 9

    def test_a_model_that_fails_still_produces_a_deck(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.openai.fails = [(500, {"error": {"message": "no"}})] * 4
        with console_client(app) as client:
            configure(client, app)
            client.get(DECK)
            drain(client, app)
            ready = client.get(DECK)
        assert ready.status_code == 200, ready.text
        cards = ready.json()["cards"]
        # The pool is already a ranked list of cards this user could be shown, so the
        # batch falls back to it. What is lost is the sentences, not the deck.
        assert len(cards) == 10
        assert all(card["rationale"] is None for card in cards)


class TestAnEmptyPool:
    def test_it_answers_an_empty_deck_and_does_not_buy_another_batch(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.tmdb.discovered = []
        with console_client(app) as client:
            configure(client, app)
            client.get(DECK)
            drain(client, app)
            first = client.get(DECK)
            second = client.get(DECK)
            usage = client.get(f"{API}/admin/usage").json()["days"][0]
        assert first.status_code == 200
        assert first.json()["cards"] == []
        assert second.status_code == 200
        assert second.json()["cards"] == []
        assert usage["generations"] == 1


class TestOneGenerationPerUser:
    def test_two_polls_arriving_together_buy_one_batch(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            first = client.get(DECK)
            second = client.get(DECK)
            drain(client, app)
            ready = client.get(DECK)
        assert (first.status_code, second.status_code) == (202, 202)
        assert ready.status_code == 200
        assert outside.openai.calls == 1


class TestARestart:
    def test_a_job_left_running_releases_the_slot_it_held(
        self, app: FastAPI, clock: FakeClock
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            services = services_of(app)
            me = client.get(f"{API}/me").json()["id"]

            async def strand() -> None:
                async with write_transaction(services.engine) as connection:
                    job = await job_repository.claim(connection, me, "batch", now=clock.now())
                    assert job is not None
                    await job_repository.start(connection, job.id, now=clock.now())

            run(client, strand)
            held = client.get(DECK)
            closed = run(client, lambda: close_abandoned_jobs(services.engine, clock))
            after = client.get(DECK)
        assert held.status_code == 202
        assert closed == 1
        # The restart's own failure is deliberately silent: it says nothing the user did
        # and nothing that will be different next time.
        assert after.status_code == 202


class TestTheWarmUp:
    def test_it_prepares_a_batch_for_somebody_who_was_here_and_not_for_anybody_else(
        self, app: FastAPI, clock: FakeClock, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            services = services_of(app)
            me = client.get(f"{API}/me").json()["id"]
            warm_up = next(job for job in app.state.jobs if job.name == "swipe_warm_up")
            run(client, warm_up.run_once)
            drain(client, app)
            ready = client.get(DECK)

            async def batches() -> Any:
                async with services.engine.connect() as connection:
                    return await batch_repository.latest_batch(connection, me, "both")

            built = run(client, batches)
        # The batch was paid for before anybody polled, so the first poll is a deck.
        assert ready.status_code == 200, ready.text
        assert built is not None
        assert outside.openai.calls == 1

    def test_it_skips_a_user_who_already_has_cards(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            client.get(DECK)
            drain(client, app)
            client.get(DECK)
            warm_up = next(job for job in app.state.jobs if job.name == "swipe_warm_up")
            run(client, warm_up.run_once)
            drain(client, app)
        assert outside.openai.calls == 1

    def test_it_does_nothing_when_the_administrator_switched_it_off(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            response = client.patch(
                f"{API}/admin/settings",
                json={"warm_up_enabled": False},
                headers=console_headers(csrf),
            )
            assert response.status_code == 200, response.text
            warm_up = next(job for job in app.state.jobs if job.name == "swipe_warm_up")
            run(client, warm_up.run_once)
            drain(client, app)
        assert outside.openai.calls == 0


class TestThePurge:
    def test_it_removes_the_cards_nobody_answered_and_keeps_the_rest(
        self, app: FastAPI, clock: FakeClock
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = _deck(client, app)
            voted = cards[0]
            response = client.post(
                f"{API}/swipe/votes",
                json={
                    "votes": [
                        {
                            "client_vote_id": "99999999-9999-4999-8999-999999999999",
                            "card_id": voted["id"],
                            "vote": "like",
                        }
                    ]
                },
                headers=console_headers(csrf),
            )
            assert response.status_code == 200, response.text
            clock.advance(CARD_RETENTION.total_seconds() + 3600)
            purge = next(job for job in app.state.jobs if job.name == "swipe_purge")
            run(client, purge.run_once)
            services = services_of(app)
            me = "unused"

            async def left() -> int:
                async with services.engine.connect() as connection:
                    rows = await connection.exec_driver_sql("SELECT COUNT(*) FROM cards")
                    return int(rows.scalar_one())

            remaining = run(client, left)
        assert remaining == 1
        assert me


def _deck(client: TestClient, app: FastAPI) -> list[dict[str, Any]]:
    assert client.get(DECK).status_code == 202
    drain(client, app)
    ready = client.get(DECK)
    assert ready.status_code == 200, ready.text
    cards: list[dict[str, Any]] = ready.json()["cards"]
    return cards
