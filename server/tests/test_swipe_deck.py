"""The deck's smaller decisions: running low, running out, and running on stale answers.

The happy path is in ``test_swipe_api`` and the failures in ``test_swipe_jobs``. What is
left is the handful of behaviours that are easy to write and easy to get quietly wrong:
the batch bought while somebody is still swiping, the second poll that must not buy
another, a cursor that could skip a like, a provider list served a fortnight late rather
than not at all, and the "not now" that has to come back in sixty days and not before.
"""

import json
import uuid
from datetime import timedelta
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
    web_login,
)
from tests.support.metadata import movie_result, tv_result
from tests.support.outside import FakeOutside
from tests.support.requests_backend import seerr_user
from tests.support.upstream import ADMIN_ID
from tests.test_contract import assert_is_problem, assert_matches_contract
from tests.test_swipe_api import configure, generate, item, vote
from tests.test_swipe_jobs import exclude_horror
from tindarr.core.errors import PendingError
from tindarr.storage import users as user_repository
from tindarr.storage.usage import PROVIDER_CACHE_LIFETIME
from tindarr.storage.users import User
from tindarr.storage.votes import SKIP_COOL_DOWN
from tindarr.swipe.deck import (
    EXHAUSTED_COOL_OFF,
    FAILURE_COOL_OFF,
    GENERATION_RETRY_MS,
    DeckService,
)
from tindarr.swipe.engine import DECK_SIZE, DeckRequest
from tindarr.swipe.generation import BatchGenerator

DECK = f"{API}/swipe/deck"
FILMS = tuple(range(301, 341))
SERIES = tuple(range(401, 421))


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


def _answer(ids: tuple[int, ...], kind: str = "movie") -> str:
    return json.dumps(
        {
            "cards": [
                {"id": tmdb_id, "media_type": kind, "rationale": "x", "pick_type": "safe"}
                for tmdb_id in ids
            ]
        }
    )


@pytest.fixture
def outside() -> FakeOutside:
    """A pool of forty films and twenty series, each with details, and a model that picks."""
    services = FakeOutside()
    services.tmdb.discover[("movie", 1)] = [
        movie_result(tmdb_id, f"Film {tmdb_id}", vote_count=900) for tmdb_id in FILMS[:20]
    ]
    services.tmdb.discover[("movie", 2)] = [
        movie_result(tmdb_id, f"Film {tmdb_id}", vote_count=900) for tmdb_id in FILMS[20:]
    ]
    services.tmdb.discover[("tv", 1)] = [
        tv_result(tmdb_id, f"Series {tmdb_id}", vote_count=900) for tmdb_id in SERIES
    ]
    for tmdb_id in FILMS:
        services.tmdb.add_details(
            "movie",
            tmdb_id,
            {"id": tmdb_id, "title": f"Film {tmdb_id}", "release_date": "2016-01-01"},
        )
    for tmdb_id in SERIES:
        services.tmdb.add_details(
            "tv",
            tmdb_id,
            {"id": tmdb_id, "name": f"Series {tmdb_id}", "first_air_date": "2019-01-01"},
        )
    services.tmdb.region_providers = [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg"}
    ]
    services.openai.answers = [
        _answer(FILMS[:10]),
        _answer(FILMS[10:20]),
        _answer(FILMS[20:30]),
        _answer(FILMS[30:40]),
    ]
    services.seerr.users = [seerr_user(5, "alex", jellyfin_user_id=ADMIN_ID)]
    return services


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet, outside: FakeOutside) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet, outside=outside)


def answer_all(
    client: TestClient, csrf: str, cards: list[dict[str, Any]], value: str = "dislike"
) -> None:
    """Vote on a whole deck, one call, as a phone emptying its queue would.

    The queue ids are fresh every time: re-using one is what a client does when it
    re-sends, and the server answers ``duplicate`` — correctly, and confusingly if a
    test did it by accident.
    """
    response = client.post(
        f"{API}/swipe/votes",
        json={
            "votes": [
                {"client_vote_id": str(uuid.uuid4()), "card_id": card["id"], "vote": value}
                for card in cards
            ]
        },
        headers=console_headers(csrf),
    )
    assert response.status_code == 200, response.text
    assert all(row["outcome"] == "stored" for row in response.json()["results"])


class TestRunningLow:
    def test_the_next_batch_is_bought_while_there_are_still_cards(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            answer_all(client, csrf, cards[:7])
            # Three left, below the low-water mark: the poll answers with them and pays
            # for the next batch behind it.
            low = client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            answer_all(client, csrf, low.json()["cards"], value="skip")
            ready = client.get(DECK)
        assert low.status_code == 200
        assert len(low.json()["cards"]) == 3
        assert outside.openai.calls == 2
        # The batch bought quietly is served without another wait.
        assert ready.status_code == 200
        assert len(ready.json()["cards"]) == DECK_SIZE

    def test_a_full_deck_buys_nothing(self, app: FastAPI, outside: FakeOutside) -> None:
        with console_client(app) as client:
            configure(client, app)
            generate(client, app)
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
        assert outside.openai.calls == 1


class TestWhatThePreferencesDo:
    def test_narrowing_to_films_hides_the_series_of_a_batch(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.openai.answers = [
            json.dumps(
                {
                    "cards": [
                        {
                            "id": tmdb_id,
                            "media_type": "movie",
                            "rationale": "x",
                            "pick_type": "safe",
                        }
                        for tmdb_id in FILMS[:5]
                    ]
                    + [
                        {"id": tmdb_id, "media_type": "tv", "rationale": "x", "pick_type": "safe"}
                        for tmdb_id in SERIES[:5]
                    ]
                }
            )
        ]
        with console_client(app) as client:
            configure(client, app)
            both = generate(client, app).json()["cards"]
            films = client.get(DECK, params={"media_type": "movie"}).json()["cards"]
        assert {card["media_type"] for card in both} == {"movie", "tv"}
        assert {card["media_type"] for card in films} == {"movie"}

    def test_a_query_parameter_is_for_this_deck_and_is_not_stored(self, app: FastAPI) -> None:
        with console_client(app) as client:
            configure(client, app)
            generate(client, app, novelty="bold")
            stored = client.get(f"{API}/swipe/preferences").json()
        assert stored["novelty"] == "balanced"

    def test_calibration_can_be_forced_off(self, app: FastAPI) -> None:
        with console_client(app) as client:
            configure(client, app)
            forced = generate(client, app, mode="normal").json()
        # The mode the deck *reports* follows the vote count, which is the client's
        # progress bar; what ``mode`` forces is the prompt the batch was built with.
        assert forced["mode"] == "calibration"
        assert len(forced["cards"]) == DECK_SIZE


class TestTheSkipCoolDown:
    def test_a_skipped_title_comes_back_after_sixty_days_and_not_before(
        self, app: FastAPI, clock: FakeClock, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            first = generate(client, app).json()["cards"]
            skipped = {card["tmdb_id"] for card in first}
            answer_all(client, csrf, first, value="skip")
            # Only the first ten of the pool are ever chosen by the fake model, so the
            # second batch can only repeat them — and must not, while they are cooling.
            outside.openai.answers = [_answer(FILMS[:10])]
            outside.openai.calls = 0
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            during = client.get(DECK).json()["cards"]
            clock.advance(60 * 60 * 24 * (SKIP_COOL_DOWN + 1))
            # Past the console session's idle limit too, so this is also "they came
            # back in two months": a new session, the same history.
            assert web_login(client).status_code == 200
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            run(client, services_of(app).batch_runner.drain)
            after = client.get(DECK).json()["cards"]
        assert not skipped & {card["tmdb_id"] for card in during}
        assert skipped & {card["tmdb_id"] for card in after}


class TestTheLikesList:
    def test_the_cursor_pages_without_skipping_or_repeating(
        self, app: FastAPI, clock: FakeClock
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            for card in cards[:4]:
                clock.advance(60)
                vote(client, csrf, item(card["id"], "like", str(uuid.uuid4())))
            first = client.get(f"{API}/swipe/likes", params={"limit": 2}).json()
            second = client.get(
                f"{API}/swipe/likes", params={"limit": 2, "cursor": first["next_cursor"]}
            ).json()
        assert first["next_cursor"] is not None
        assert [like["tmdb_id"] for like in first["likes"]] != [
            like["tmdb_id"] for like in second["likes"]
        ]
        assert len({like["tmdb_id"] for like in first["likes"] + second["likes"]}) == 4

    def test_a_cursor_this_server_never_issued_is_refused(self, app: FastAPI) -> None:
        with console_client(app) as client:
            configure(client, app)
            response = client.get(f"{API}/swipe/likes", params={"cursor": "tomorrow"})
        assert_is_problem(response, 400, "validation_error")

    def test_a_backend_that_is_down_costs_the_badge_and_not_the_list(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app, requests=True)
            card = generate(client, app).json()["cards"][0]
            vote(client, csrf, item(card["id"], "like"))
            outside.seerr.offline = True
            likes = client.get(f"{API}/swipe/likes")
            deck = client.get(DECK)
        assert likes.status_code == 200
        assert likes.json()["likes"][0]["availability"] == "none"
        assert deck.status_code == 200
        assert deck.json()["cards"][0]["availability"] == "none"


class TestTheProviderCache:
    def test_a_stale_list_beats_no_list_at_all(
        self, app: FastAPI, clock: FakeClock, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            fresh = client.get(f"{API}/swipe/providers").json()
            clock.advance(PROVIDER_CACHE_LIFETIME.total_seconds() + 60)
            assert web_login(client).status_code == 200
            outside.tmdb.offline = True
            stale = client.get(f"{API}/swipe/providers")
        assert stale.status_code == 200
        assert stale.json() == fresh

    def test_nothing_cached_and_tmdb_down_is_an_error(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            outside.tmdb.offline = True
            response = client.get(f"{API}/swipe/providers")
        assert_is_problem(response, 503, "metadata_unreachable")


class TestTheSeenWarning:
    def test_it_lights_up_once_enough_recent_cards_were_already_known(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            answer_all(client, csrf, cards, value="seen_liked")
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            deck = client.get(DECK)
        assert deck.status_code == 200
        assert deck.json()["seen_ratio_warning"] is True


class TestTheProfileFailing:
    def test_the_reason_is_kept_and_the_profile_is_not_touched(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            client.put(
                f"{API}/swipe/profile",
                json={"text": "Mine."},
                headers=console_headers(csrf),
            )
            answer_all(client, csrf, cards, value="like")
            # Every attempt the provider will make, refused: the adapter retries once.
            outside.openai.fails = [(401, {"error": {"message": "no"}})] * 6
            client.post(f"{API}/swipe/profile/refresh", headers=console_headers(csrf))
            run(client, services_of(app).profile_runner.drain)
            state = client.get(f"{API}/swipe/profile")
        assert_matches_contract("/api/v1/swipe/profile", "get", state)
        body = state.json()
        assert body["refresh_error"] == "llm_auth_failed"
        assert body["profile"]["text"] == "Mine."
        assert body["refreshing"] is False


class _Scheduler:
    """A batch scheduler the test drives: full, broken, or both."""

    def __init__(self, *, capacity: bool = True, broken: bool = False) -> None:
        self.capacity = capacity
        self.broken = broken
        self.submitted: list[str] = []

    def has_capacity(self) -> bool:
        """Whether this stand-in claims the server has room."""
        return self.capacity

    def submit(self, user: User, request: DeckRequest, job_id: str) -> None:
        """Take the job, or fail the way a worker that cannot start one would."""
        if self.broken:
            raise RuntimeError("no worker")
        self.submitted.append(job_id)


def _deck_service(app: FastAPI, scheduler: _Scheduler) -> DeckService:
    """The real deck service, with the test's scheduler in place of the runner."""
    services = services_of(app)
    generator = BatchGenerator(services.engine, services.swipe, services.clock)
    return DeckService(services.engine, services.swipe, generator, scheduler, services.clock)


async def _only_user(app: FastAPI) -> User:
    async with services_of(app).engine.connect() as connection:
        return (await user_repository.list_all(connection))[0]


class TestTheBusyServer:
    """The two paths a runner can refuse a claimed generation on.

    Driven against the real ``DeckService`` with a scheduler of the test's own, because
    what is being checked is the **decision**, and the only way to reach it through HTTP
    would be to fill the server's two slots with generations that never finish.
    """

    def test_a_poll_waits_longer_when_every_slot_is_taken(self, app: FastAPI) -> None:
        with console_client(app) as client:
            configure(client, app)
            deck = _deck_service(app, _Scheduler(capacity=False))
            user = run(client, lambda: _only_user(app))
            with pytest.raises(PendingError) as raised:
                run(client, lambda: deck.deck(user, DeckRequest()))
        assert raised.value.retry_after_ms > GENERATION_RETRY_MS

    def test_a_scheduler_that_cannot_take_the_job_does_not_keep_the_slot(
        self, app: FastAPI
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            broken = _deck_service(app, _Scheduler(broken=True))
            user = run(client, lambda: _only_user(app))
            with pytest.raises(RuntimeError):
                run(client, lambda: broken.deck(user, DeckRequest()))
            # The claimed job was closed rather than left holding the slot, and the
            # failure is silent on the next poll: the 500 already reported it once.
            after = client.get(DECK)
        assert after.status_code == 202


class TestWhatTheReviewFound:
    """The five ways the deck could spend or lose something, each pinned by a test.

    Every one of these was found by an adversarial review of the lot rather than by a
    failing test, which is why they are grouped: the shape they share is that the happy
    path is untouched and the damage only shows on the second or the hundredth call.
    """

    def test_a_failing_provider_does_not_spend_the_day_in_half_a_minute(
        self, app: FastAPI, clock: FakeClock, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            exclude_horror(client, csrf)
            outside.tmdb.offline = True
            for _ in range(6):
                client.get(DECK)
                run(client, services_of(app).batch_runner.drain)
            spent = client.get(f"{API}/admin/usage").json()["days"]
            clock.advance(FAILURE_COOL_OFF.total_seconds() + 1)
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            after = client.get(f"{API}/admin/usage").json()["days"]
        # Six polls, one generation: the rest met the cool-off. Past it, one more.
        assert spent[0]["generations"] == 1
        assert after[0]["generations"] == 2

    def test_an_empty_first_batch_is_not_a_blank_deck_for_ever(
        self, app: FastAPI, clock: FakeClock, outside: FakeOutside
    ) -> None:
        outside.tmdb.discover.clear()
        outside.tmdb.discovered = []
        with console_client(app) as client:
            configure(client, app)
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            blank = client.get(DECK)
            clock.advance(EXHAUSTED_COOL_OFF.total_seconds() + 60)
            assert web_login(client).status_code == 200
            again = client.get(DECK)
        assert blank.status_code == 200
        assert blank.json()["cards"] == []
        # The client is told *why* it is blank rather than left to guess.
        assert blank.json()["exhausted"] is True
        # And the deck tries again by itself: a pool is not empty for ever.
        assert again.status_code == 202

    def test_a_queued_vote_cannot_overwrite_a_newer_opinion(
        self, app: FastAPI, clock: FakeClock
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            card = generate(client, app).json()["cards"][0]
            served_at = clock.now()
            clock.advance(3600)
            fresh = client.post(
                f"{API}/swipe/votes",
                json={
                    "votes": [
                        {"client_vote_id": str(uuid.uuid4()), "card_id": card["id"], "vote": "like"}
                    ]
                },
                headers=console_headers(csrf),
            )
            assert fresh.status_code == 200, fresh.text
            # The same card, answered three weeks ago on a phone that was in a drawer.
            late = client.post(
                f"{API}/swipe/votes",
                json={
                    "votes": [
                        {
                            "client_vote_id": str(uuid.uuid4()),
                            "card_id": card["id"],
                            "vote": "dislike",
                            # After the card was served, before the answer above.
                            "voted_at": (served_at + timedelta(minutes=1)).isoformat(),
                        }
                    ]
                },
                headers=console_headers(csrf),
            )
            assert late.status_code == 200, late.text
            stats = client.get(f"{API}/swipe/stats").json()
        assert stats["likes"] == 1
        assert stats["dislikes"] == 0

    def test_a_backdated_skip_does_not_shorten_its_own_cool_down(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            first = generate(client, app).json()["cards"]
            skipped = {card["tmdb_id"] for card in first}
            response = client.post(
                f"{API}/swipe/votes",
                json={
                    "votes": [
                        {
                            "client_vote_id": str(uuid.uuid4()),
                            "card_id": card["id"],
                            "vote": "skip",
                            # Long before the card existed, which is the whole point.
                            "voted_at": "1970-01-01T00:00:00Z",
                        }
                        for card in first
                    ]
                },
                headers=console_headers(csrf),
            )
            assert response.status_code == 200, response.text
            outside.openai.answers = [_answer(FILMS[:10])]
            outside.openai.calls = 0
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            after = client.get(DECK).json()["cards"]
        assert not skipped & {card["tmdb_id"] for card in after}

    def test_one_queue_of_likes_pages_all_the_way_through(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            # One submission: every item is stored at the same instant, which is what
            # a timestamp-only cursor cannot page past.
            answer_all(client, csrf, cards, value="like")
            seen: list[int] = []
            cursor: str | None = None
            for _ in range(5):
                params: dict[str, Any] = {"limit": 4}
                if cursor is not None:
                    params["cursor"] = cursor
                page = client.get(f"{API}/swipe/likes", params=params).json()
                seen.extend(like["tmdb_id"] for like in page["likes"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        assert len(seen) == len(set(seen)) == len(cards)

    def test_a_model_outage_is_counted_even_though_the_deck_survives_it(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.openai.fails = [(500, {"error": {"message": "no"}})] * 4
        with console_client(app) as client:
            configure(client, app)
            client.get(DECK)
            run(client, services_of(app).batch_runner.drain)
            deck = client.get(DECK)
            usage = client.get(f"{API}/admin/usage").json()["days"][0]
        assert deck.status_code == 200
        assert len(deck.json()["cards"]) == DECK_SIZE
        # A full deck, and a failure an administrator can see on the usage page.
        assert usage["failures"] == 1

    def test_the_console_counts_votes_and_generations_per_user(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            card = generate(client, app).json()["cards"][0]
            vote(client, csrf, item(card["id"], "like"))
            users = client.get(f"{API}/admin/users").json()["users"]
        me = next(user for user in users if user["votes"] > 0)
        assert me["votes"] == 1
        assert me["generations_today"] == 1
