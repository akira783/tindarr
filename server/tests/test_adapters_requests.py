"""The Seerr family behind the request backend port (roadmap step 3).

The two things worth breaking a build over are here: a request is filed **as the user**
who asked for it, and the user list is read as **nobody in particular**. Everything else
— seasons, availability, the failure paths — follows.
"""

from typing import Any

import pytest

from tests.support import media_user
from tests.support.requests_backend import (
    SEERR_API_KEY,
    SEERR_URL,
    FakeSeerr,
    body_of,
    seerr_user,
)
from tindarr.adapters.seerr import ON_BEHALF_HEADER, SeerrBackend
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import MediaServerKind
from tindarr.ports.request_backend import BackendUser
from tindarr.ports.titles import TitleRef

pytestmark = pytest.mark.anyio

ALEX_JELLYFIN_ID = "8a1b2c3d4e5f60718293a4b5c6d7e8f9"
ARRIVAL = TitleRef("movie", 329865)
SEVERANCE = TitleRef("tv", 95396)
ROBIN = BackendUser(id=3, display_name="Robin")


def backend(
    fake: FakeSeerr,
    kind: MediaServerKind = "jellyfin",
    api_key: str = SEERR_API_KEY,
    **fields: Any,
) -> SeerrBackend:
    """A real Seerr adapter talking to ``fake``."""
    return SeerrBackend(SEERR_URL, api_key, kind, transport=fake.transport, **fields)


@pytest.fixture
def seerr() -> FakeSeerr:
    """A fake Seerr with three users."""
    fake = FakeSeerr()
    fake.users = [
        seerr_user(1, "Admin", jellyfin_user_id="0" * 32),
        # The backend stores the id with dashes and in capitals; Tindarr does not.
        seerr_user(2, "Alex", jellyfin_user_id="8A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9"),
        seerr_user(3, "Robin", plex_id=4242),
    ]
    return fake


# --- the connection test ------------------------------------------------------------


async def test_a_good_connector_tests_ok(seerr: FakeSeerr) -> None:
    check = await backend(seerr).test()
    assert check.ok
    assert check.server_version == "2.7.3"


async def test_a_wrong_key_is_unauthorized(seerr: FakeSeerr) -> None:
    assert (await backend(seerr, api_key="wrong").test()).health == "unauthorized"


async def test_a_key_the_backend_refuses_is_unauthorized(seerr: FakeSeerr) -> None:
    seerr.key_refused = True
    assert (await backend(seerr).test()).health == "unauthorized"


async def test_an_unreachable_backend_is_unreachable(seerr: FakeSeerr) -> None:
    seerr.offline = True
    assert (await backend(seerr).test()).health == "unreachable"


async def test_something_that_is_not_a_seerr_is_unexpected(seerr: FakeSeerr) -> None:
    seerr.garbage.add("/api/v1/status")
    assert (await backend(seerr).test()).health == "unexpected_response"


async def test_a_backend_that_errors_on_the_user_list_is_unexpected(seerr: FakeSeerr) -> None:
    seerr.fails["/api/v1/user"] = 500
    assert (await backend(seerr).test()).health == "unexpected_response"


async def test_a_user_list_that_is_not_json_is_unexpected(seerr: FakeSeerr) -> None:
    seerr.garbage.add("/api/v1/user")
    assert (await backend(seerr).test()).health == "unexpected_response"


async def test_a_backend_with_no_status_endpoint_is_unexpected(seerr: FakeSeerr) -> None:
    seerr.fails["/api/v1/status"] = 404
    assert (await backend(seerr).test()).health == "unexpected_response"


# --- matching a user ----------------------------------------------------------------


async def test_a_jellyfin_user_is_matched_on_a_normalised_id(seerr: FakeSeerr) -> None:
    found = await backend(seerr).find_user(media_user(ALEX_JELLYFIN_ID, "Alex"))

    assert found is not None
    assert found.id == 2
    assert found.display_name == "Alex"
    assert found.media_server_user_id == ALEX_JELLYFIN_ID


async def test_a_plex_user_is_matched_on_the_account_id(seerr: FakeSeerr) -> None:
    found = await backend(seerr, "plex").find_user(media_user("4242", "Robin"))

    assert found is not None
    assert found.id == 3


async def test_the_user_list_is_never_read_on_somebodys_behalf(seerr: FakeSeerr) -> None:
    await backend(seerr).find_user(media_user(ALEX_JELLYFIN_ID, "Alex"))

    listing = seerr.request_of("/api/v1/user")
    assert ON_BEHALF_HEADER.lower() not in {name.lower() for name in listing.headers}
    assert listing.headers["x-api-key"] == SEERR_API_KEY


async def test_a_user_the_backend_does_not_know_matches_nobody(seerr: FakeSeerr) -> None:
    assert await backend(seerr).find_user(media_user("f" * 32, "Sam")) is None


async def test_a_plex_id_never_matches_a_jellyfin_user(seerr: FakeSeerr) -> None:
    # The same digits as Robin's plex id, asked for as a Jellyfin account.
    assert await backend(seerr).find_user(media_user("4" * 32, "Robin")) is None


async def test_an_id_of_the_wrong_shape_matches_nobody(seerr: FakeSeerr) -> None:
    seerr.users = [seerr_user(9, "Broken", jellyfin_user_id="not-an-id")]
    assert await backend(seerr).find_user(media_user(ALEX_JELLYFIN_ID, "Alex")) is None


async def test_a_row_without_an_id_is_skipped(seerr: FakeSeerr) -> None:
    seerr.users = [
        {"displayName": "No id", "jellyfinUserId": ALEX_JELLYFIN_ID},
        seerr_user(2, "Alex", jellyfin_user_id=ALEX_JELLYFIN_ID),
    ]
    found = await backend(seerr).find_user(media_user(ALEX_JELLYFIN_ID, "Alex"))
    assert found is not None
    assert found.id == 2


async def test_a_user_named_only_by_their_media_server_is_still_named(
    seerr: FakeSeerr,
) -> None:
    seerr.users = [{"id": 5, "jellyfinUsername": "Sam", "jellyfinUserId": ALEX_JELLYFIN_ID}]
    found = await backend(seerr).find_user(media_user(ALEX_JELLYFIN_ID, "Sam"))
    assert found is not None
    assert found.display_name == "Sam"


async def test_the_user_list_is_paged(seerr: FakeSeerr) -> None:
    seerr.users = [seerr_user(n, f"User {n}", jellyfin_user_id=f"{n:032x}") for n in range(250)]

    found = await backend(seerr).find_user(media_user(f"{249:032x}", "User 249"))

    assert found is not None
    assert found.id == 249
    skips = [r.url.params.get("skip") for r in seerr.requests if r.url.path == "/api/v1/user"]
    assert skips == ["0", "100", "200"]


async def test_a_backend_that_fails_the_listing_is_a_backend_error(seerr: FakeSeerr) -> None:
    seerr.fails["/api/v1/user"] = 502

    with pytest.raises(ProblemError) as failure:
        await backend(seerr).find_user(media_user(ALEX_JELLYFIN_ID, "Alex"))
    assert failure.value.code == "request_backend_error"


# --- filing a request ---------------------------------------------------------------


async def test_a_request_is_filed_as_the_user_who_asked(seerr: FakeSeerr) -> None:
    result = await backend(seerr).request(ARRIVAL, ROBIN)

    sent = seerr.request_of("/api/v1/request")
    assert sent.headers[ON_BEHALF_HEADER] == "3"
    assert sent.headers["x-api-key"] == SEERR_API_KEY
    assert body_of(sent) == {"mediaType": "movie", "mediaId": 329865, "is4k": False}
    assert result.status == "queued"
    assert result.request_id == 7


async def test_a_films_body_never_carries_seasons(seerr: FakeSeerr) -> None:
    await backend(seerr).request(ARRIVAL, ROBIN)
    assert "seasons" not in body_of(seerr.request_of("/api/v1/request"))


async def test_a_series_asks_for_every_season_by_default(seerr: FakeSeerr) -> None:
    await backend(seerr).request(SEVERANCE, ROBIN)
    assert body_of(seerr.request_of("/api/v1/request"))["seasons"] == "all"


async def test_a_series_can_be_limited_to_the_first_season(seerr: FakeSeerr) -> None:
    await backend(seerr, tv_seasons="first").request(SEVERANCE, ROBIN)
    assert body_of(seerr.request_of("/api/v1/request"))["seasons"] == [1]


async def test_a_request_waiting_for_an_administrator_says_so(seerr: FakeSeerr) -> None:
    seerr.request_answer = (201, {"id": 9, "status": 1, "media": {"status": 2}})

    result = await backend(seerr).request(ARRIVAL, ROBIN)

    assert result.status == "awaiting_approval"
    assert result.availability == "requested"


async def test_an_accepted_request_counts_as_queued(seerr: FakeSeerr) -> None:
    seerr.request_answer = (202, {"id": 9, "status": 2, "media": {"status": 3}})
    assert (await backend(seerr).request(ARRIVAL, ROBIN)).status == "queued"


async def test_a_title_the_backend_already_has_is_already_available(
    seerr: FakeSeerr,
) -> None:
    seerr.request_answer = (201, {"id": 9, "status": 2, "media": {"status": 5}})

    result = await backend(seerr).request(ARRIVAL, ROBIN)

    assert result.status == "already_available"
    assert result.availability == "available"


async def test_a_title_already_requested_is_not_requested_twice(seerr: FakeSeerr) -> None:
    seerr.request_answer = (409, {"message": "Request for this media already exists."})
    assert (await backend(seerr).request(ARRIVAL, ROBIN)).status == "already_requested"


async def test_a_user_without_the_permission_is_refused(seerr: FakeSeerr) -> None:
    seerr.request_answer = (403, {"message": "You do not have permission to do that."})

    with pytest.raises(ProblemError) as failure:
        await backend(seerr).request(ARRIVAL, ROBIN)
    assert failure.value.code == "request_not_allowed"
    # Nothing the backend wrote is passed on.
    assert "permission to do that" not in (failure.value.detail or "")


@pytest.mark.parametrize(
    "message", ["Request quota exceeded.", "You have reached your request limit."]
)
async def test_a_spent_quota_is_its_own_answer(seerr: FakeSeerr, message: str) -> None:
    seerr.request_answer = (403, {"message": message})

    with pytest.raises(ProblemError) as failure:
        await backend(seerr).request(ARRIVAL, ROBIN)
    assert failure.value.code == "quota_exceeded"


async def test_a_refusal_with_no_readable_body_is_a_missing_permission(
    seerr: FakeSeerr,
) -> None:
    seerr.request_answer = (403, {})
    with pytest.raises(ProblemError) as failure:
        await backend(seerr).request(ARRIVAL, ROBIN)
    assert failure.value.code == "request_not_allowed"


async def test_a_backend_that_breaks_on_a_request_is_a_backend_error(
    seerr: FakeSeerr,
) -> None:
    seerr.request_answer = (500, {"message": "boom"})
    with pytest.raises(ProblemError) as failure:
        await backend(seerr).request(ARRIVAL, ROBIN)
    assert failure.value.code == "request_backend_error"


async def test_an_unreadable_success_is_a_backend_error(seerr: FakeSeerr) -> None:
    seerr.garbage.add("/api/v1/request")
    with pytest.raises(ProblemError) as failure:
        await backend(seerr).request(ARRIVAL, ROBIN)
    assert failure.value.code == "request_backend_error"


async def test_an_unreachable_backend_on_a_request_is_a_backend_error(
    seerr: FakeSeerr,
) -> None:
    seerr.offline = True
    with pytest.raises(ProblemError) as failure:
        await backend(seerr).request(ARRIVAL, ROBIN)
    assert failure.value.code == "request_backend_error"


# --- availability -------------------------------------------------------------------


async def test_availability_maps_the_backends_own_states(seerr: FakeSeerr) -> None:
    seerr.availability = {
        ("movie", 1): 1,
        ("movie", 2): 2,
        ("movie", 3): 3,
        ("movie", 4): 4,
        ("movie", 5): 5,
        ("movie", 6): 99,
    }
    titles = [TitleRef("movie", number) for number in range(1, 7)]

    found = await backend(seerr).status(titles)

    assert [found[title] for title in titles] == [
        "none",
        "requested",
        "processing",
        "partially_available",
        "available",
        "none",
    ]


async def test_a_title_the_backend_never_heard_of_is_not_requested(seerr: FakeSeerr) -> None:
    assert await backend(seerr).status([ARRIVAL]) == {ARRIVAL: "none"}


async def test_asking_for_nothing_calls_nothing(seerr: FakeSeerr) -> None:
    assert await backend(seerr).status([]) == {}
    assert seerr.requests == []


async def test_the_same_title_twice_is_asked_once(seerr: FakeSeerr) -> None:
    seerr.availability = {("movie", 329865): 5}

    found = await backend(seerr).status([ARRIVAL, ARRIVAL])

    assert found == {ARRIVAL: "available"}
    assert len([r for r in seerr.requests if r.url.path.endswith("/329865")]) == 1


async def test_a_failure_costs_a_badge_not_the_deck(seerr: FakeSeerr) -> None:
    seerr.offline = True
    assert await backend(seerr).status([ARRIVAL, SEVERANCE]) == {
        ARRIVAL: "none",
        SEVERANCE: "none",
    }
