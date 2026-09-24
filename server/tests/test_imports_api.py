"""The import and calibration endpoints, over HTTP, with a real TMDb adapter behind them.

Two things are checked here that no unit test can: that an upload is bounded before it
is read, and that nothing in this pair of features can be reached across accounts —
console-only where it changes something, the caller's own rows where it reads.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import (
    API,
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
)
from tests.support.metadata import TMDB_API_KEY, movie_result, tv_result
from tests.support.outside import FakeOutside
from tests.test_contract import assert_is_problem, assert_matches_contract
from tindarr.swipe.imports import MAX_UPLOAD_BYTES

IMPORTS = f"{API}/swipe/imports"
GRID = f"{API}/swipe/calibration/grid"
NETFLIX = (
    b"Title,Date\n"
    b'"Heroes: Saison 1: Genesis","9/10/26"\n'
    b'"Heroes: Saison 1: Don\'t Look Back","9/11/26"\n'
)
UPLOAD_HEADERS = {"Content-Type": "text/csv"}


@pytest.fixture
def internet() -> FakeInternet:
    return seed_jellyfin(FakeInternet())


@pytest.fixture
def outside() -> FakeOutside:
    """A TMDb that knows one series, and has a wall of famous films to discover."""
    services = FakeOutside()
    services.tmdb.add_search("tv", "Heroes", tv_result(1639, "Heroes", 2006))
    services.tmdb.add_details("tv", 1639, {"id": 1639, "name": "Heroes", "number_of_episodes": 78})
    # Famous enough to be worth a poster: the wall's floor is in vote counts.
    services.tmdb.discovered = [
        movie_result(index, f"Famous {index}", vote_count=20_000) for index in range(1, 9)
    ]
    return services


@pytest.fixture
def app(data_dir: Path, clock: FakeClock, internet: FakeInternet, outside: FakeOutside) -> FastAPI:
    return build_app(data_dir, clock=clock, internet=internet, outside=outside)


def with_tmdb(client: TestClient, app: FastAPI) -> str:
    """Set the server up and configure TMDb; return the console's CSRF token."""
    csrf = set_up_server(client, app)
    response = client.put(
        f"{API}/admin/connectors/tmdb",
        json={"connector": "tmdb", "api_key": TMDB_API_KEY},
        headers=console_headers(csrf),
    )
    assert response.status_code == 200, response.text
    return csrf


def upload(client: TestClient, csrf: str, payload: bytes = NETFLIX) -> Any:
    """Send one file the way the console does: the bytes, as the body."""
    return client.post(IMPORTS, content=payload, headers=console_headers(csrf) | UPLOAD_HEADERS)


def finish(client: TestClient, app: FastAPI) -> None:
    """Wait for the background half of an upload."""
    run(client, services_of(app).import_runner.drain)


class TestUploading:
    """What arrives, and what is refused before anything is read."""

    def test_an_export_is_accepted_and_identified(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            response = upload(client, csrf)
            assert response.status_code == 202, response.text
            assert_matches_contract(IMPORTS, "post", response)
            body = response.json()
            assert body["format"] == "netflix"
            assert body["status"] == "running"
            finish(client, app)
            done = client.get(f"{IMPORTS}/{body['id']}")
            assert_matches_contract(f"{IMPORTS}/{{import_id}}", "get", done)
        assert done.json()["status"] == "complete"
        assert done.json()["matched"] == 1
        assert done.json()["titles"] == 1

    def test_a_file_nobody_can_read_is_refused_without_a_row(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            response = upload(client, csrf, b"who,what\n1,2\n")
            assert_is_problem(response, 400, "import_unreadable")
            assert client.get(IMPORTS).json()["imports"] == []

    def test_an_upload_past_the_cap_is_refused(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            payload = b"Title,Date\n" + b'"x","9/10/26"\n' * (MAX_UPLOAD_BYTES // 13 + 2)
            assert_is_problem(upload(client, csrf, payload), 413, "import_too_large")

    def test_a_lying_content_length_is_refused_too(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            response = client.post(
                IMPORTS,
                content=NETFLIX,
                headers=console_headers(csrf)
                | UPLOAD_HEADERS
                | {"Content-Length": str(MAX_UPLOAD_BYTES + 1)},
            )
            assert_is_problem(response, 413, "import_too_large")

    def test_a_body_that_never_declared_its_length(self, app: FastAPI) -> None:
        # The interesting direction: no Content-Length at all (chunked), and a body
        # past the cap. Only the counter on the stream can stop this one.
        def chunks() -> Iterator[bytes]:
            row = b'"Film","9/10/26"\n'
            yield b"Title,Date\n"
            for _ in range(MAX_UPLOAD_BYTES // len(row) + 32):
                yield row

        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            response = client.post(
                IMPORTS, content=chunks(), headers=console_headers(csrf) | UPLOAD_HEADERS
            )
            assert "content-length" not in {key.lower() for key in dict(response.request.headers)}
            assert_is_problem(response, 413, "import_too_large")

    def test_without_tmdb_nothing_can_be_identified(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = set_up_server(client, app)
            assert_is_problem(upload(client, csrf), 409, "tmdb_not_configured")

    def test_too_many_uploads_in_an_hour(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            codes: set[int] = set()
            for _ in range(8):
                codes.add(upload(client, csrf, b"who,what\n1,2\n").status_code)
            finish(client, app)
        assert 429 in codes


class TestAuthorisation:
    """Changing is console only; reading is the caller's own rows and nobody else's."""

    def test_a_phone_cannot_upload(self, app: FastAPI) -> None:
        with console_client(app) as client:
            with_tmdb(client, app)
            token = app_login(client, "robin", USER_PASSWORD).json()["access_token"]
            response = client.post(
                IMPORTS, content=NETFLIX, headers={"Authorization": f"Bearer {token}"}
            )
        assert_is_problem(response, 401, "unauthorized")

    def test_an_upload_without_a_csrf_token_is_refused(self, app: FastAPI) -> None:
        with console_client(app) as client:
            with_tmdb(client, app)
            response = client.post(IMPORTS, content=NETFLIX, headers=UPLOAD_HEADERS)
        assert_is_problem(response, 403, "csrf_failed")

    def test_another_user_s_import_is_not_found(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(client, csrf).json()
            finish(client, app)
            token = app_login(client, "robin", USER_PASSWORD).json()["access_token"]
            bearer = {"Authorization": f"Bearer {token}"}
            mine = client.get(IMPORTS, headers=bearer)
            theirs = client.get(f"{IMPORTS}/{created['id']}", headers=bearer)
        assert mine.json()["imports"] == []
        assert_is_problem(theirs, 404, "not_found")
        assert_matches_contract(IMPORTS, "get", mine)


class TestReviewQueue:
    """The rows nobody could identify, and answering them."""

    @pytest.fixture
    def outside(self) -> FakeOutside:
        services = FakeOutside()
        # Close enough to offer, not close enough to write down.
        services.tmdb.add_search(
            "tv", "Mushoku Tensei", tv_result(94664, "Mushoku Tensei: Jobless Reincarnation")
        )
        services.tmdb.add_details(
            "tv", 94664, {"id": 94664, "name": "Mushoku", "number_of_episodes": 23}
        )
        return services

    def queued(self, client: TestClient, app: FastAPI) -> tuple[str, dict[str, Any]]:
        csrf = with_tmdb(client, app)
        created = upload(
            client, csrf, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
        ).json()
        finish(client, app)
        path = f"{IMPORTS}/{created['id']}/review"
        response = client.get(path)
        assert_matches_contract(f"{IMPORTS}/{{import_id}}/review", "get", response)
        return created["id"], response.json()

    def test_an_uncertain_row_is_queued_with_its_candidates(self, app: FastAPI) -> None:
        with console_client(app) as client:
            _, body = self.queued(client, app)
        assert body["pending"] == 1
        entry = body["entries"][0]
        assert entry["query"] == "Mushoku Tensei"
        assert entry["candidates"][0]["tmdb_id"] == 94664
        assert entry["candidates"][0]["similarity"] < 0.85

    def test_accepting_one_writes_it_and_empties_the_queue(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(
                client, csrf, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
            ).json()
            finish(client, app)
            path = f"{IMPORTS}/{created['id']}/review"
            entry = client.get(path).json()["entries"][0]
            answer = client.post(
                f"{path}/{entry['id']}",
                json={"decision": "accept", "title": {"media_type": "tv", "tmdb_id": 94664}},
                headers=console_headers(csrf),
            )
            assert answer.status_code == 204, answer.text
            assert client.get(path).json()["pending"] == 0
            # Answering again: the entry is no longer in the queue.
            repeat = client.post(
                f"{path}/{entry['id']}",
                json={"decision": "reject"},
                headers=console_headers(csrf),
            )
        assert_is_problem(repeat, 404, "not_found")

    def test_a_title_the_entry_never_offered(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(
                client, csrf, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
            ).json()
            finish(client, app)
            path = f"{IMPORTS}/{created['id']}/review"
            entry = client.get(path).json()["entries"][0]
            response = client.post(
                f"{path}/{entry['id']}",
                json={"decision": "accept", "title": {"media_type": "movie", "tmdb_id": 999}},
                headers=console_headers(csrf),
            )
        assert_is_problem(response, 400, "validation_error")

    def test_accepting_without_saying_which_title(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(
                client, csrf, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
            ).json()
            finish(client, app)
            path = f"{IMPORTS}/{created['id']}/review"
            entry = client.get(path).json()["entries"][0]
            response = client.post(
                f"{path}/{entry['id']}", json={"decision": "accept"}, headers=console_headers(csrf)
            )
        assert_is_problem(response, 400, "validation_error")

    def test_an_entry_answered_through_another_queue_s_path(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(
                client, csrf, b'Title,Date\n"Mushoku Tensei: Saison 1: A","9/10/26"\n'
            ).json()
            finish(client, app)
            entry = client.get(f"{IMPORTS}/{created['id']}/review").json()["entries"][0]
            response = client.post(
                f"{IMPORTS}/another/review/{entry['id']}",
                json={"decision": "reject"},
                headers=console_headers(csrf),
            )
        assert_is_problem(response, 404, "not_found")

    def test_the_queue_of_an_import_that_is_not_theirs(self, app: FastAPI) -> None:
        with console_client(app) as client:
            with_tmdb(client, app)
            response = client.get(f"{IMPORTS}/nope/review")
        assert_is_problem(response, 404, "not_found")


class TestForgetting:
    """Undoing an import."""

    def test_it_removes_the_import_and_its_history(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(client, csrf).json()
            finish(client, app)
            gone = client.delete(f"{IMPORTS}/{created['id']}", headers=console_headers(csrf))
            assert gone.status_code == 204, gone.text
            after = client.get(f"{IMPORTS}/{created['id']}")
        assert_is_problem(after, 404, "not_found")

    def test_a_phone_cannot_forget_one(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            created = upload(client, csrf).json()
            finish(client, app)
            token = app_login(client, "robin", USER_PASSWORD).json()["access_token"]
            response = client.delete(
                f"{IMPORTS}/{created['id']}", headers={"Authorization": f"Bearer {token}"}
            )
        assert_is_problem(response, 401, "unauthorized")


class TestCalibrationGrid:
    """The wall, and what ticking it does."""

    def test_it_serves_famous_posters_and_never_repeats_an_answer(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            wall = client.get(GRID, params={"size": 6})
            assert wall.status_code == 200, wall.text
            assert_matches_contract(GRID, "get", wall)
            titles = wall.json()["titles"]
            assert titles
            answered = titles[0]
            recorded = client.post(
                GRID,
                json={
                    "answers": [
                        {
                            "media_type": answered["media_type"],
                            "tmdb_id": answered["tmdb_id"],
                            "seen": True,
                        }
                    ]
                },
                headers=console_headers(csrf),
            )
            assert recorded.status_code == 200, recorded.text
            assert_matches_contract(GRID, "post", recorded)
            assert recorded.json()["recorded"] == 1
            again = client.get(GRID, params={"size": 6})
        assert answered["tmdb_id"] not in {row["tmdb_id"] for row in again.json()["titles"]}

    def test_a_no_keeps_the_poster_off_the_next_wall(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            answered = client.get(GRID, params={"size": 6}).json()["titles"][0]
            client.post(
                GRID,
                json={
                    "answers": [
                        {
                            "media_type": answered["media_type"],
                            "tmdb_id": answered["tmdb_id"],
                            "seen": False,
                        }
                    ]
                },
                headers=console_headers(csrf),
            )
            again = client.get(GRID, params={"size": 6})
        assert answered["tmdb_id"] not in {row["tmdb_id"] for row in again.json()["titles"]}

    def test_a_phone_may_tick_a_wall(self, app: FastAPI) -> None:
        with console_client(app) as client:
            with_tmdb(client, app)
            token = app_login(client, "robin", USER_PASSWORD).json()["access_token"]
            bearer = {"Authorization": f"Bearer {token}"}
            wall = client.get(GRID, params={"size": 4}, headers=bearer)
            assert wall.status_code == 200, wall.text
            answered = wall.json()["titles"][0]
            recorded = client.post(
                GRID,
                json={
                    "answers": [
                        {
                            "media_type": answered["media_type"],
                            "tmdb_id": answered["tmdb_id"],
                            "seen": True,
                        }
                    ]
                },
                headers=bearer,
            )
        assert recorded.status_code == 200, recorded.text

    def test_too_many_walls_in_an_hour(self, app: FastAPI) -> None:
        # The endpoint takes any TMDb id, not only the ones a wall offered, so this is
        # the only thing between a household member and a database of history rows.
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            answer = {"media_type": "movie", "tmdb_id": 27205, "seen": True}
            codes: set[int] = set()
            for _ in range(25):
                codes.add(
                    client.post(
                        GRID, json={"answers": [answer]}, headers=console_headers(csrf)
                    ).status_code
                )
        assert 429 in codes

    def test_an_empty_answer_list(self, app: FastAPI) -> None:
        with console_client(app) as client:
            csrf = with_tmdb(client, app)
            response = client.post(GRID, json={"answers": []}, headers=console_headers(csrf))
        assert_is_problem(response, 400, "validation_error")

    def test_without_tmdb_there_is_no_wall(self, app: FastAPI) -> None:
        with console_client(app) as client:
            set_up_server(client, app)
            assert_is_problem(client.get(GRID), 409, "tmdb_not_configured")
