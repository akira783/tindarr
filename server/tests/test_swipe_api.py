"""The deck and everything a swipe produces, over HTTP, with the real adapters behind it.

The whole path: a fake TMDb builds a pool, the real OpenAI SDK talks to a fake endpoint
that answers a batch, the real Seerr adapter files a request, and the console and the
app both drive it. What these tests are here to pin down, beyond "it works":

- **nobody reaches anybody else's cards.** Voting with another user's card id is the
  same answer as voting with a made-up one, and the lists are all scoped to the caller;
- **a vote cannot be forged.** The pick type and the title on a stored vote come off the
  server's own row, never off the request;
- **a request needs a card.** A title this caller was never offered is refused before
  the backend is called at all;
- **a queue is idempotent.** The same ``client_vote_id`` twice is one vote, even after
  the user undid it in between.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    API,
    USER_NAME,
    USER_PASSWORD,
    FakeClock,
    FakeInternet,
    app_login,
    build_app,
    console_client,
    console_headers,
    run,
    seed_jellyfin,
    services_of,
    set_up_server,
    web_login,
)
from tests.support.ai import OPENAI_KEY
from tests.support.metadata import OMDB_API_KEY, TMDB_API_KEY, movie_result
from tests.support.outside import FakeOutside
from tests.support.requests_backend import SEERR_API_KEY, SEERR_URL, seerr_user
from tests.support.upstream import ADMIN_ID
from tests.test_contract import assert_is_problem, assert_matches_contract

DECK = f"{API}/swipe/deck"
VOTES = f"{API}/swipe/votes"
POOL_IDS = tuple(range(101, 131))


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


def _chosen(*ids: int) -> str:
    """What the model answers: a selection of ids it was offered, with a sentence each."""
    return json.dumps(
        {
            "cards": [
                {
                    "id": tmdb_id,
                    "media_type": "movie",
                    "rationale": f"Because of {tmdb_id}.",
                    "pick_type": "explore" if index else "safe",
                }
                for index, tmdb_id in enumerate(ids)
            ]
        }
    )


@pytest.fixture
def outside() -> FakeOutside:
    """A TMDb with a pool, one described title per candidate, and a model that picks."""
    services = FakeOutside()
    services.tmdb.discovered = [
        movie_result(tmdb_id, f"Candidate {tmdb_id}", 2016 + (tmdb_id % 5), vote_count=900)
        for tmdb_id in POOL_IDS
    ]
    for tmdb_id in POOL_IDS:
        services.tmdb.add_details(
            "movie",
            tmdb_id,
            {
                "id": tmdb_id,
                "title": f"Candidate {tmdb_id}",
                "original_title": f"Candidate {tmdb_id}",
                "release_date": "2016-01-01",
                "overview": "A film.",
                "genres": [{"id": 878, "name": "Science Fiction"}],
                "runtime": 100,
                "poster_path": f"/{tmdb_id}.jpg",
                "backdrop_path": f"/b{tmdb_id}.jpg",
                "vote_average": 7.4,
                "vote_count": 900,
                "imdb_id": f"tt{tmdb_id:07d}",
            },
        )
        services.tmdb.providers[("movie", tmdb_id)] = {
            "FR": {
                "flatrate": [{"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg"}],
                "rent": [{"provider_id": 2, "provider_name": "Apple TV", "logo_path": "/a.jpg"}],
            }
        }
        services.tmdb.videos[("movie", tmdb_id)] = [
            {
                "site": "YouTube",
                "type": "Trailer",
                "key": "abc123XY",
                "name": "Trailer",
                "iso_639_1": "en",
            }
        ]
    services.tmdb.region_providers = [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg"},
        {"provider_id": 337, "provider_name": "Disney Plus", "logo_path": "/d.jpg"},
    ]
    services.openai.answers = [_chosen(*POOL_IDS[:10])]
    services.seerr.users = [seerr_user(5, "alex", jellyfin_user_id=ADMIN_ID)]
    return services


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet, outside: FakeOutside) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet, outside=outside)


def configure(client: TestClient, app: FastAPI, **extra: bool) -> str:
    """Set the server up and configure TMDb and an AI provider; return the CSRF token."""
    csrf = set_up_server(client, app)
    put(client, csrf, "tmdb", {"connector": "tmdb", "api_key": TMDB_API_KEY})
    put(
        client,
        csrf,
        "llm",
        {
            "connector": "llm",
            "provider": "openai",
            "api_key": OPENAI_KEY,
            "model": "model-a",
        },
    )
    patch = client.patch(
        f"{API}/admin/settings",
        json={"streaming_region": "FR"},
        headers=console_headers(csrf),
    )
    assert patch.status_code == 200, patch.text
    if extra.get("requests"):
        put(
            client,
            csrf,
            "requests",
            {"connector": "requests", "url": SEERR_URL, "api_key": SEERR_API_KEY},
        )
    if extra.get("ratings"):
        put(client, csrf, "omdb", {"connector": "omdb", "api_key": OMDB_API_KEY})
    return csrf


def put(client: TestClient, csrf: str, kind: str, body: dict[str, Any]) -> None:
    response = client.put(
        f"{API}/admin/connectors/{kind}", json=body, headers=console_headers(csrf)
    )
    assert response.status_code == 200, response.text


def generate(client: TestClient, app: FastAPI, url: str = DECK, **params: Any) -> Any:
    """Poll the deck the way a client does: once for the 202, then for the cards."""
    pending = client.get(url, params=params)
    assert pending.status_code == 202, pending.text
    assert pending.json()["retry_after_ms"] >= 250
    assert_matches_contract("/api/v1/swipe/deck", "get", pending)
    run(client, services_of(app).batch_runner.drain)
    ready = client.get(url, params=params)
    assert ready.status_code == 200, ready.text
    return ready


def vote(client: TestClient, csrf: str | None, *items: dict[str, Any], **headers: Any) -> Any:
    return client.post(VOTES, json={"votes": list(items)}, headers=console_headers(csrf, **headers))


def item(
    card_id: str, value: str = "like", client_vote_id: str = "11111111-1111-4111-8111-111111111111"
) -> dict[str, Any]:
    return {"client_vote_id": client_vote_id, "card_id": card_id, "vote": value}


class TestTheDeck:
    def test_the_first_poll_pays_for_a_batch_and_the_second_shows_it(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            ready = generate(client, app)
        body = ready.json()
        assert_matches_contract("/api/v1/swipe/deck", "get", ready)
        assert len(body["cards"]) == 10
        assert body["mode"] == "calibration"
        assert body["calibration"] == {"done": 0, "target": 15, "complete": False}
        first = body["cards"][0]
        assert first["title"] == "Candidate 101"
        assert first["rationale"] == "Because of 101."
        assert first["pick_type"] == "safe"
        assert first["trailer"] == {
            "site": "youtube",
            "key": "abc123XY",
            "name": "Trailer",
            "language": "en",
        }
        assert first["ratings"]["tmdb"] == 7.4
        # One model call for ten cards is the whole of ADR 0013's yield argument.
        assert outside.openai.calls == 1

    def test_polling_again_returns_the_same_cards_and_spends_nothing(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            first = generate(client, app).json()
            again = client.get(DECK)
        assert again.status_code == 200
        assert [card["id"] for card in again.json()["cards"]] == [
            card["id"] for card in first["cards"]
        ]
        assert outside.openai.calls == 1

    def test_the_subscribed_badge_follows_the_preferences_not_the_card(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            before = generate(client, app).json()["cards"][0]["providers"]
            assert [offer["subscribed"] for offer in before] == [False, False]
            patch = client.patch(
                f"{API}/swipe/preferences",
                json={"streaming_services": [8]},
                headers=console_headers(csrf),
            )
            assert patch.status_code == 200, patch.text
            after = client.get(DECK).json()["cards"][0]["providers"]
        # The rental offer never counts, whatever the user ticked: "on your services"
        # asks whether it is included in something they already pay for.
        assert [(offer["offer"], offer["subscribed"]) for offer in after] == [
            ("subscription", True),
            ("rent", False),
        ]

    def test_a_deck_without_tmdb_or_a_model_says_which_one_is_missing(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = set_up_server(client, app)
            without_anything = client.get(DECK)
            put(client, csrf, "tmdb", {"connector": "tmdb", "api_key": TMDB_API_KEY})
            without_a_model = client.get(DECK)
        assert_is_problem(without_anything, 409, "tmdb_not_configured")
        assert_is_problem(without_a_model, 409, "llm_not_configured")

    def test_the_status_endpoint_answers_before_any_card_exists(self, app: FastAPI) -> None:
        with console_client(app) as client:
            configure(client, app, requests=True)
            response = client.get(f"{API}/swipe/status")
        assert response.status_code == 200
        assert_matches_contract("/api/v1/swipe/status", "get", response)
        body = response.json()
        assert body["llm_configured"] is True
        assert body["llm_provider"] == "openai"
        assert body["tmdb_configured"] is True
        assert body["requests_enabled"] is True
        assert body["streaming_region"] == "FR"
        assert body["votes"] == 0
        assert body["generations_left_today"] == 10


class TestVoting:
    def test_a_vote_carries_only_a_card_id_and_the_server_fills_the_rest(
        self, app: FastAPI
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            explore = next(card for card in cards if card["pick_type"] == "explore")
            response = vote(client, csrf, item(explore["id"], "like"))
            assert response.status_code == 200, response.text
            assert_matches_contract("/api/v1/swipe/votes", "post", response)
            assert response.json()["results"][0]["outcome"] == "stored"
            stats = client.get(f"{API}/swipe/stats").json()
        assert stats["total"] == 1
        assert stats["likes"] == 1
        # The pick type is the row's, not the request's: nothing was sent about it.
        assert stats["by_pick_type"] == {"explore": {"total": 1, "likes": 1}}

    def test_the_same_queued_item_twice_is_one_vote_even_after_an_undo(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            card = generate(client, app).json()["cards"][0]
            assert vote(client, csrf, item(card["id"])).json()["results"][0]["outcome"] == "stored"
            undo = client.delete(
                f"{VOTES}/{card['media_type']}/{card['tmdb_id']}", headers=console_headers(csrf)
            )
            assert undo.status_code == 204
            again = vote(client, csrf, item(card["id"]))
            stats = client.get(f"{API}/swipe/stats").json()
        assert again.json()["results"][0]["outcome"] == "duplicate"
        assert stats["total"] == 0

    def test_an_unknown_card_is_rejected_and_the_others_are_not(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            response = vote(
                client,
                csrf,
                item("not-a-card", "dislike", "22222222-2222-4222-8222-222222222222"),
                item(cards[0]["id"], "like", "33333333-3333-4333-8333-333333333333"),
            )
        outcomes = response.json()["results"]
        assert [row["outcome"] for row in outcomes] == ["rejected", "stored"]
        assert outcomes[0]["code"] == "card_not_found"

    def test_a_card_that_left_the_deck_can_still_be_voted_on(
        self, app: FastAPI, clock: FakeClock
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            card = generate(client, app).json()["cards"][0]
            # Past the card's 24 hours — and past the console session's idle limit, so
            # this is also the phone-in-a-tunnel case: a new session, an old card.
            clock.advance(60 * 60 * 25)
            fresh = web_login(client)
            assert fresh.status_code == 200, fresh.text
            csrf = fresh.json()["csrf_token"]
            assert client.get(DECK).status_code in (200, 202)
            response = vote(client, csrf, item(card["id"]))
            stats = client.get(f"{API}/swipe/stats").json()
        assert response.json()["results"][0]["outcome"] == "stored"
        assert stats["likes"] == 1

    def test_undoing_a_vote_nobody_cast_is_a_404(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            response = client.delete(f"{VOTES}/movie/999", headers=console_headers(csrf))
        assert_is_problem(response, 404, "not_found")

    def test_a_skip_is_counted_apart_from_every_rate(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            vote(client, csrf, item(cards[0]["id"], "skip"))
            vote(
                client,
                csrf,
                item(cards[1]["id"], "like", "44444444-4444-4444-8444-444444444444"),
            )
            stats = client.get(f"{API}/swipe/stats")
        assert_matches_contract("/api/v1/swipe/stats", "get", stats)
        body = stats.json()
        assert body["skips"] == 1
        assert body["total"] == 1
        assert body["like_rate"] == 1.0

    def test_resetting_votes_keeps_the_profile(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            card = generate(client, app).json()["cards"][0]
            vote(client, csrf, item(card["id"]))
            saved = client.put(
                f"{API}/swipe/profile",
                json={"text": "Loves: heists"},
                headers=console_headers(csrf),
            )
            assert saved.status_code == 200, saved.text
            reset = client.delete(f"{VOTES}?confirm=reset-votes", headers=console_headers(csrf))
            profile = client.get(f"{API}/swipe/profile")
        assert reset.status_code == 200
        assert reset.json() == {"deleted": 1}
        assert profile.json()["profile"]["text"] == "Loves: heists"
        assert profile.json()["profile"]["user_edited"] is True


class TestAcrossAccounts:
    def test_one_user_cannot_vote_on_another_user_s_card(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            mine = generate(client, app).json()["cards"][0]
            signed_in = app_login(client, USER_NAME, USER_PASSWORD)
            assert signed_in.status_code == 200, signed_in.text
            token = signed_in.json()["access_token"]
            stolen = client.post(
                VOTES,
                json={"votes": [item(mine["id"])]},
                headers={"Authorization": f"Bearer {token}"},
            )
            mine_still = client.get(f"{API}/swipe/stats", headers=console_headers(csrf)).json()
        assert stolen.status_code == 200
        assert stolen.json()["results"][0] == {
            "client_vote_id": "11111111-1111-4111-8111-111111111111",
            "outcome": "rejected",
            "code": "card_not_found",
            "request_status": None,
        }
        assert mine_still["total"] == 0

    def test_each_user_has_their_own_deck_likes_and_profile(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            mine = generate(client, app).json()["cards"][0]
            vote(client, csrf, item(mine["id"]))
            token = app_login(client, USER_NAME, USER_PASSWORD).json()["access_token"]
            bearer = {"Authorization": f"Bearer {token}"}
            theirs = client.get(f"{API}/swipe/likes", headers=bearer)
            their_stats = client.get(f"{API}/swipe/stats", headers=bearer)
            mine_likes = client.get(f"{API}/swipe/likes", headers=console_headers(csrf))
        assert theirs.json()["likes"] == []
        assert their_stats.json()["total"] == 0
        assert [like["tmdb_id"] for like in mine_likes.json()["likes"]] == [mine["tmdb_id"]]


class TestRequests:
    def test_a_title_nobody_offered_is_refused_before_the_backend_is_called(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app, requests=True)
            before = len(outside.seerr.requests)
            response = client.post(
                f"{API}/swipe/requests",
                json={"media_type": "movie", "tmdb_id": 424242},
                headers=console_headers(csrf),
            )
        assert_is_problem(response, 403, "title_not_offered")
        assert len(outside.seerr.requests) == before

    def test_a_served_title_is_filed_as_the_caller_s_own_backend_user(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app, requests=True)
            card = generate(client, app).json()["cards"][0]
            response = client.post(
                f"{API}/swipe/requests",
                json={"media_type": card["media_type"], "tmdb_id": card["tmdb_id"]},
                headers=console_headers(csrf),
            )
        assert response.status_code == 200, response.text
        assert_matches_contract("/api/v1/swipe/requests", "post", response)
        assert response.json()["request_status"] in ("queued", "awaiting_approval")
        filed = outside.seerr.request_of("/api/v1/request")
        assert filed.headers["x-api-user"] == "5"

    def test_auto_request_files_a_like_without_being_asked(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app, requests=True)
            client.patch(
                f"{API}/swipe/preferences",
                json={"auto_request": True},
                headers=console_headers(csrf),
            )
            card = generate(client, app).json()["cards"][0]
            response = vote(client, csrf, item(card["id"], "like"))
            likes = client.get(f"{API}/swipe/likes")
        assert response.json()["results"][0]["request_status"] is not None
        assert_matches_contract("/api/v1/swipe/likes", "get", likes)
        assert likes.json()["likes"][0]["requested"] is True

    def test_a_backend_that_does_not_know_the_user_refuses_rather_than_using_the_key(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.seerr.users = []
        with console_client(app) as client:
            csrf = configure(client, app, requests=True)
            card = generate(client, app).json()["cards"][0]
            response = client.post(
                f"{API}/swipe/requests",
                json={"media_type": card["media_type"], "tmdb_id": card["tmdb_id"]},
                headers=console_headers(csrf),
            )
        assert_is_problem(response, 403, "no_backend_user")
        assert not [row for row in outside.seerr.requests if row.url.path == "/api/v1/request"]


class TestProfileAndPreferences:
    def test_the_profile_starts_empty_and_keeps_what_the_user_wrote(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.openai.answers = [
            _chosen(*POOL_IDS[:10]),
            json.dumps({"loves": ["Heists"], "avoids": [], "nuances": ["Not on Mondays"]}),
        ]
        with console_client(app) as client:
            csrf = configure(client, app)
            empty = client.get(f"{API}/swipe/profile")
            assert_matches_contract("/api/v1/swipe/profile", "get", empty)
            assert empty.json()["profile"] is None
            saved = client.put(
                f"{API}/swipe/profile",
                json={"text": "I only watch in French."},
                headers=console_headers(csrf),
            )
            assert saved.status_code == 200, saved.text
            started = client.post(f"{API}/swipe/profile/refresh", headers=console_headers(csrf))
            assert started.status_code == 202, started.text
            assert_matches_contract("/api/v1/swipe/profile/refresh", "post", started)
            run(client, services_of(app).profile_runner.drain)
            after = client.get(f"{API}/swipe/profile").json()
        # Under the vote floor there is nothing to summarise, so the rewrite writes
        # nothing — and what the user wrote is still exactly what they wrote.
        assert after["profile"]["text"] == "I only watch in French."
        assert after["profile"]["user_edited"] is True
        assert after["refreshing"] is False

    def test_a_rewrite_adds_to_the_user_s_text_without_replacing_it(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        outside.openai.answers = [
            _chosen(*POOL_IDS[:10]),
            json.dumps({"loves": ["Science fiction"], "avoids": ["Musicals"], "nuances": []}),
        ]
        with console_client(app) as client:
            csrf = configure(client, app)
            cards = generate(client, app).json()["cards"]
            client.put(
                f"{API}/swipe/profile",
                json={"text": "I only watch in French."},
                headers=console_headers(csrf),
            )
            for index, card in enumerate(cards[:6]):
                vote(
                    client,
                    csrf,
                    item(card["id"], "like", f"5555555{index}-5555-4555-8555-555555555555"),
                )
            client.post(f"{API}/swipe/profile/refresh", headers=console_headers(csrf))
            run(client, services_of(app).profile_runner.drain)
            after = client.get(f"{API}/swipe/profile").json()
        assert after["profile"]["text"].startswith("I only watch in French.")
        assert "Loves:\n- Science fiction" in after["profile"]["text"]
        assert "Avoids:\n- Musicals" in after["profile"]["text"]

    def test_preferences_default_to_the_server_and_change_one_field_at_a_time(
        self, app: FastAPI
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            defaults = client.get(f"{API}/swipe/preferences")
            assert_matches_contract("/api/v1/swipe/preferences", "get", defaults)
            assert defaults.json() == {
                "media_type": "both",
                "novelty": "balanced",
                "auto_request": False,
                "language": "en",
                "streaming_services": [],
            }
            patched = client.patch(
                f"{API}/swipe/preferences",
                json={"novelty": "bold", "streaming_services": [8, 8]},
                headers=console_headers(csrf),
            )
        assert patched.status_code == 200, patched.text
        assert patched.json()["novelty"] == "bold"
        assert patched.json()["streaming_services"] == [8]
        assert patched.json()["media_type"] == "both"

    def test_an_empty_patch_is_refused(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            response = client.patch(
                f"{API}/swipe/preferences", json={}, headers=console_headers(csrf)
            )
        assert_is_problem(response, 400, "validation_error")

    def test_the_region_s_providers_come_from_tmdb_once_a_week(
        self, app: FastAPI, outside: FakeOutside
    ) -> None:
        with console_client(app) as client:
            configure(client, app)
            first = client.get(f"{API}/swipe/providers")
            calls = len([row for row in outside.tmdb.requests if "watch/providers" in row.url.path])
            second = client.get(f"{API}/swipe/providers")
            after = len([row for row in outside.tmdb.requests if "watch/providers" in row.url.path])
        assert first.status_code == 200, first.text
        assert_matches_contract("/api/v1/swipe/providers", "get", first)
        assert first.json()["region"] == "FR"
        assert [option["name"] for option in first.json()["providers"]] == [
            "Disney Plus",
            "Netflix",
        ]
        assert second.json() == first.json()
        assert after == calls


class TestUsage:
    def test_the_usage_page_shows_what_a_batch_cost_and_only_to_an_admin(
        self, app: FastAPI
    ) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            generate(client, app)
            response = client.get(f"{API}/admin/usage")
            token = app_login(client, USER_NAME, USER_PASSWORD).json()["access_token"]
            refused = client.get(f"{API}/admin/usage", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        assert_matches_contract("/api/v1/admin/usage", "get", response)
        row = response.json()["days"][0]
        assert row["generations"] == 1
        assert row["input_tokens"] == 11
        assert row["output_tokens"] == 7
        assert row["failures"] == 0
        assert refused.status_code == 401
        assert csrf


class TestLimits:
    def test_a_user_with_no_generations_left_is_told_so(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            me = client.get(f"{API}/me").json()
            patched = client.patch(
                f"{API}/admin/users/{me['id']}",
                json={"daily_generation_limit": 0},
                headers=console_headers(csrf),
            )
            assert patched.status_code == 200, patched.text
            response = client.get(DECK)
        assert_is_problem(response, 429, "daily_limit_reached")
        assert response.json()["retry_after_ms"] > 0

    def test_the_cap_counts_the_batch_that_was_already_paid_for(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = configure(client, app)
            me = client.get(f"{API}/me").json()
            client.patch(
                f"{API}/admin/users/{me['id']}",
                json={"daily_generation_limit": 1},
                headers=console_headers(csrf),
            )
            generate(client, app)
            status = client.get(f"{API}/swipe/status").json()
        assert status["generations_left_today"] == 0
