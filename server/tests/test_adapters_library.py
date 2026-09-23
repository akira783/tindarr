"""The library and engagement side of the media server adapters (roadmap step 3).

The real adapters run against the fake Jellyfin, Emby and Plex servers of
``tests.support.upstream``, through ``httpx2.MockTransport``: no network, no container,
and the wire details — the two spellings of the user-items route, Plex's container
range, ``ProviderIds.Tmdb`` as a string — are exercised exactly as in production.

What these tests are really guarding is the shape of the *signal*, not the parsing: a
play count must never become a rewatch, a series must tip at 60 % of its episodes, and a
title nobody ever opened must produce nothing at all.
"""

from datetime import timedelta
from typing import Any

import httpx2
import pytest

from tests.support import (
    ADMIN_API_KEY,
    MACHINE_ID,
    MEDIA_SERVER_URL,
    PLEX_SERVER_URL,
    FakeClock,
    FakeInternet,
    FakeMediaBrowser,
    media_user,
)
from tests.support.upstream import PlayedEpisode, ResumeEntry
from tindarr.adapters.emby import EmbyServer
from tindarr.adapters.jellyfin import JellyfinServer
from tindarr.adapters.mediabrowser import MediaBrowserServer
from tindarr.adapters.plex import PlexServer
from tindarr.adapters.plextv import PlexTvClient
from tindarr.core.errors import ProblemError
from tindarr.ports.media_server import Engagement, LibraryItem, MediaServerConnection, MediaUser
from tindarr.ports.titles import TitleRef

pytestmark = pytest.mark.anyio

USER = media_user("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Alex")
OWNER_TOKEN = "owner-token"
OWNER_ACCOUNT = "4242"


def when(clock: FakeClock, days_ago: float) -> str:
    """A Media Browser timestamp that many days before the clock's now."""
    moment = clock.now() - timedelta(days=days_ago)
    # Seven fractional digits, as both products serialise .NET dates.
    return moment.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def jellyfin(fake: FakeMediaBrowser, clock: FakeClock) -> JellyfinServer:
    """A real Jellyfin adapter talking to ``fake``."""
    return JellyfinServer(_connection("jellyfin", MEDIA_SERVER_URL), fake.transport, clock)


def emby(fake: FakeMediaBrowser, clock: FakeClock) -> EmbyServer:
    """A real Emby adapter talking to ``fake``."""
    return EmbyServer(_connection("emby", MEDIA_SERVER_URL), fake.transport, clock)


def _connection(kind: Any, url: str) -> MediaServerConnection:
    return MediaServerConnection(
        kind=kind, url=url, secret=ADMIN_API_KEY, verify_tls=True, device_id="install-1"
    )


@pytest.fixture
def media() -> FakeMediaBrowser:
    """A fake Jellyfin with one administrator and the user the tests watch with."""
    fake = FakeMediaBrowser()
    fake.add_user("Alex", "pw", user_id=USER.id)
    return fake


# --- the library --------------------------------------------------------------------


async def test_the_library_is_read_with_the_admin_key_and_keyed_by_tmdb_id(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865", year=2016)
    media.add_library_item("2", "Severance", "Series", tmdb_id="95396", year=2022)

    index = await jellyfin(media, clock).library_ids()

    assert index.owns(TitleRef("movie", 329865))
    assert index.owns(TitleRef("tv", 95396))
    assert len(index) == 2
    found = index.find(TitleRef("movie", 329865))
    assert found is not None
    assert found.name == "Arrival"
    assert found.year == 2016
    # The administrator key, not a user token, and no user in the query.
    listing = next(r for r in media.requests if r.url.path == "/Items")
    assert ADMIN_API_KEY in listing.headers["authorization"]
    assert "userId" not in listing.url.query.decode()


@pytest.mark.parametrize("raw", [None, "", "not-a-number", "0", "-3"])
async def test_an_item_without_a_usable_tmdb_id_is_kept_but_matches_nothing(
    media: FakeMediaBrowser, clock: FakeClock, raw: str | None
) -> None:
    media.add_library_item("1", "Holiday 2011", "Movie", tmdb_id=raw)

    index = await jellyfin(media, clock).library_ids()

    assert len(index) == 1
    assert index.refs == frozenset()


async def test_the_library_is_paged(media: FakeMediaBrowser, clock: FakeClock) -> None:
    for number in range(1, 1202):
        media.add_library_item(str(number), f"Film {number}", "Movie", tmdb_id=str(number))

    index = await jellyfin(media, clock).library_ids()

    assert len(index) == 1201
    starts = [r.url.params.get("StartIndex") for r in media.requests if r.url.path == "/Items"]
    assert starts == ["0", "500", "1000"]


async def test_emby_falls_back_to_the_route_it_has(clock: FakeClock) -> None:
    fake = FakeMediaBrowser(kind="emby", legacy_user_items=True, modern_user_items=False)
    fake.add_user("Alex", "pw", user_id=USER.id)
    fake.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    fake.resume.append(ResumeEntry(item_id="1", user_id=USER.id, percentage=40.0))

    engagements = await emby(fake, clock).engagement(USER)

    assert [e.item.item_id for e in engagements] == ["1"]
    assert any(r.url.path == f"/Users/{USER.id}/Items" for r in fake.requests)


async def test_a_server_that_answers_something_else_is_unreachable(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.fails["/Items"] = 500

    with pytest.raises(ProblemError) as failure:
        await jellyfin(media, clock).library_ids()
    assert failure.value.code == "media_server_unreachable"


async def test_a_listing_that_is_not_a_listing_is_unreachable(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/Items":
            return httpx2.Response(200, json={"Items": "not a list"})
        return media.handle(request)

    server = JellyfinServer(
        _connection("jellyfin", MEDIA_SERVER_URL), httpx2.MockTransport(answer), clock
    )
    with pytest.raises(ProblemError) as failure:
        await server.library_ids()
    assert failure.value.code == "media_server_unreachable"


async def test_a_body_that_is_not_json_is_unreachable(clock: FakeClock) -> None:
    server = JellyfinServer(
        _connection("jellyfin", MEDIA_SERVER_URL),
        httpx2.MockTransport(lambda _r: httpx2.Response(200, content=b"<html>nope")),
        clock,
    )
    with pytest.raises(ProblemError) as failure:
        await server.library_ids()
    assert failure.value.code == "media_server_unreachable"


# --- deep links ---------------------------------------------------------------------


def test_deep_links_point_at_the_configured_address(clock: FakeClock) -> None:
    item = LibraryItem(kind="movie", item_id="42", name="Arrival")
    assert jellyfin(FakeMediaBrowser(), clock).deep_link(item) == (
        f"{MEDIA_SERVER_URL}/web/#/details?id=42"
    )
    assert emby(FakeMediaBrowser(), clock).deep_link(item) == (
        f"{MEDIA_SERVER_URL}/web/index.html#!/item?id=42"
    )


# --- engagement: films --------------------------------------------------------------


async def engagement_of(
    media: FakeMediaBrowser, clock: FakeClock, user: MediaUser = USER
) -> dict[str, Engagement]:
    """Run the real adapter and index the result by item id."""
    return {e.item.item_id: e for e in await jellyfin(media, clock).engagement(user)}


async def test_a_film_the_server_calls_played_is_watched(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    row.user_data[USER.id] = {"Played": True, "LastPlayedDate": when(clock, 400)}

    found = await engagement_of(media, clock)

    assert found["1"].state == "watched"
    assert found["1"].progress == 1.0


async def test_a_film_left_near_the_end_is_watched(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    row.user_data[USER.id] = {"Played": False, "PlayedPercentage": 93.0}

    assert (await engagement_of(media, clock))["1"].state == "watched"


async def test_a_film_started_recently_is_in_progress(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    media.resume.append(
        ResumeEntry(item_id="1", user_id=USER.id, percentage=22.0, last_played=when(clock, 3))
    )

    found = (await engagement_of(media, clock))["1"]
    assert found.state == "in_progress"
    assert found.progress == pytest.approx(0.22)


async def test_a_film_dropped_long_ago_is_abandoned(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    media.resume.append(
        ResumeEntry(item_id="1", user_id=USER.id, percentage=12.0, last_played=when(clock, 120))
    )

    assert (await engagement_of(media, clock))["1"].state == "abandoned"


async def test_a_film_with_progress_but_no_date_is_only_paused(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    media.resume.append(ResumeEntry(item_id="1", user_id=USER.id, percentage=12.0))

    assert (await engagement_of(media, clock))["1"].state == "paused"


async def test_a_film_nobody_opened_produces_nothing(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")

    assert await engagement_of(media, clock) == {}


async def test_play_counts_are_never_a_signal(media: FakeMediaBrowser, clock: FakeClock) -> None:
    """A debrid setup reports nine plays for a film nobody finished; it stays abandoned."""
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    row = media.library[0]
    row.user_data[USER.id] = {"Played": False, "PlayCount": 9}
    media.resume.append(
        ResumeEntry(item_id="1", user_id=USER.id, percentage=8.0, last_played=when(clock, 200))
    )

    found = (await engagement_of(media, clock))["1"]
    assert found.state == "abandoned"
    assert found.progress == pytest.approx(0.08)


# --- engagement: series -------------------------------------------------------------


def watch_episodes(media: FakeMediaBrowser, clock: FakeClock, count: int, days_ago: float) -> None:
    """Mark ``count`` episodes of series ``10`` as played by the test's user."""
    for number in range(count):
        media.played_episodes.append(
            PlayedEpisode(
                episode_id=f"e{number}",
                series_id="10",
                user_id=USER.id,
                last_played=when(clock, days_ago),
            )
        )


async def test_a_series_tips_to_watched_at_sixty_percent(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("10", "Severance", "Series", tmdb_id="95396")
    row.user_data[USER.id] = {"UnplayedItemCount": 4}
    watched_long_ago = 300
    watch_episodes(media, clock, 6, watched_long_ago)

    found = (await engagement_of(media, clock))["10"]

    # Six of ten, untouched for ten months: still "watched most of it", never abandoned.
    assert found.state == "mostly_watched"
    assert found.episodes_played == 6
    assert found.episodes_total == 10
    assert found.progress == pytest.approx(0.6)


async def test_a_series_almost_finished_is_watched(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("10", "Severance", "Series", tmdb_id="95396")
    row.user_data[USER.id] = {"UnplayedItemCount": 1}
    watch_episodes(media, clock, 9, 300)

    assert (await engagement_of(media, clock))["10"].state == "watched"


async def test_a_series_barely_started_and_long_untouched_is_abandoned(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("10", "Severance", "Series", tmdb_id="95396")
    row.user_data[USER.id] = {"UnplayedItemCount": 18}
    watch_episodes(media, clock, 2, 200)

    assert (await engagement_of(media, clock))["10"].state == "abandoned"


async def test_a_long_series_paused_a_long_way_in_is_not_abandoned(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("10", "ER", "Series", tmdb_id="1416")
    row.user_data[USER.id] = {"UnplayedItemCount": 65}
    watch_episodes(media, clock, 35, 200)

    assert (await engagement_of(media, clock))["10"].state == "paused"


async def test_a_series_watched_this_month_is_in_progress(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("10", "Severance", "Series", tmdb_id="95396")
    row.user_data[USER.id] = {"UnplayedItemCount": 18}
    watch_episodes(media, clock, 2, 5)

    assert (await engagement_of(media, clock))["10"].state == "in_progress"


async def test_a_series_whose_total_is_unknown_is_judged_on_recency_alone(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("10", "Severance", "Series", tmdb_id="95396")
    watch_episodes(media, clock, 30, 4)

    found = (await engagement_of(media, clock))["10"]
    assert found.episodes_total is None
    assert found.state == "in_progress"


async def test_an_episode_in_the_resume_list_counts_for_its_series(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    media.add_library_item("10", "Severance", "Series", tmdb_id="95396")
    media.resume.append(
        ResumeEntry(
            item_id="e7",
            series_id="10",
            item_type="Episode",
            user_id=USER.id,
            percentage=50.0,
            last_played=when(clock, 2),
        )
    )

    assert (await engagement_of(media, clock))["10"].state == "in_progress"


async def test_another_users_history_is_not_read(media: FakeMediaBrowser, clock: FakeClock) -> None:
    media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    media.resume.append(ResumeEntry(item_id="1", user_id="b" * 32, percentage=40.0))

    assert await engagement_of(media, clock) == {}


async def test_a_seven_digit_timestamp_is_understood(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    row.user_data[USER.id] = {"Played": True, "LastPlayedDate": "2026-09-20T18:04:05.1234567Z"}

    found = (await engagement_of(media, clock))["1"]
    assert found.last_played_at is not None
    assert found.last_played_at.year == 2026


async def test_an_unreadable_timestamp_is_ignored(
    media: FakeMediaBrowser, clock: FakeClock
) -> None:
    row = media.add_library_item("1", "Arrival", "Movie", tmdb_id="329865")
    row.user_data[USER.id] = {"Played": False, "PlayedPercentage": 30.0, "LastPlayedDate": "soon"}

    found = (await engagement_of(media, clock))["1"]
    assert found.last_played_at is None
    assert found.state == "paused"


def test_the_item_path_is_the_only_product_difference() -> None:
    assert MediaBrowserServer.item_path != EmbyServer.item_path


# --- Plex ---------------------------------------------------------------------------


def plex_server(internet: FakeInternet, clock: FakeClock) -> PlexServer:
    """A real Plex adapter talking to the fake server and the fake plex.tv."""
    return PlexServer(
        _plex_connection(),
        PlexTvClient(internet.plex_tv.transport),
        internet.transport,
        clock,
    )


def _plex_connection() -> MediaServerConnection:
    return MediaServerConnection(
        kind="plex",
        url=PLEX_SERVER_URL,
        secret=OWNER_TOKEN,
        verify_tls=True,
        device_id="install-1",
    )


def plex_movie(rating_key: str, title: str, tmdb_id: str, **state: Any) -> dict[str, Any]:
    """A Plex ``Metadata`` row for a film."""
    return {
        "ratingKey": rating_key,
        "title": title,
        "type": "movie",
        "year": 2016,
        "duration": 7_200_000,
        "Guid": [{"id": "imdb://tt2543164"}, {"id": f"tmdb://{tmdb_id}?lang=en"}],
        **state,
    }


def plex_show(rating_key: str, title: str, tmdb_id: str, **state: Any) -> dict[str, Any]:
    """A Plex ``Metadata`` row for a series."""
    return {
        "ratingKey": rating_key,
        "title": title,
        "type": "show",
        "year": 2022,
        "Guid": [{"id": f"tmdb://{tmdb_id}"}],
        **state,
    }


@pytest.fixture
def plex() -> FakeInternet:
    """A fake Plex server with two libraries and an owner account on plex.tv."""
    internet = FakeInternet()
    internet.plex_tv.add_account(OWNER_TOKEN, OWNER_ACCOUNT, "Robin")
    internet.plex_sections = [
        {"key": "1", "type": "movie", "title": "Films"},
        {"key": "2", "type": "show", "title": "Series"},
        {"key": "3", "type": "artist", "title": "Music"},
    ]
    internet.plex_items = {
        "1": [plex_movie("100", "Arrival", "329865")],
        "2": [plex_show("200", "Severance", "95396", leafCount=10)],
        "3": [{"ratingKey": "300", "title": "Bowie", "type": "artist"}],
    }
    return internet


async def test_the_plex_library_reads_every_film_and_series_library(
    plex: FakeInternet, clock: FakeClock
) -> None:
    index = await plex_server(plex, clock).library_ids()

    assert index.refs == {TitleRef("movie", 329865), TitleRef("tv", 95396)}
    # The music library is never asked for.
    asked = [r.url.path for r in plex.plex_requests]
    assert "/library/sections/1/all" in asked
    assert "/library/sections/3/all" not in asked


async def test_a_plex_deep_link_needs_the_machine_identifier(
    plex: FakeInternet, clock: FakeClock
) -> None:
    server = plex_server(plex, clock)
    item = LibraryItem(kind="movie", item_id="100", name="Arrival", tmdb_id=329865)

    assert server.deep_link(item) is None
    await server.library_ids()
    link = server.deep_link(item)
    assert link is not None
    assert link == (
        f"https://app.plex.tv/desktop/#!/server/{MACHINE_ID}"
        "/details?key=%2Flibrary%2Fmetadata%2F100"
    )


async def test_the_owners_own_progress_is_read_from_the_library(
    plex: FakeInternet, clock: FakeClock
) -> None:
    viewed_at = int((clock.now() - timedelta(days=3)).timestamp())
    plex.plex_items["1"] = [
        plex_movie("100", "Arrival", "329865", viewOffset=1_800_000, lastViewedAt=viewed_at)
    ]
    plex.plex_items["2"] = [
        plex_show(
            "200", "Severance", "95396", leafCount=10, viewedLeafCount=7, lastViewedAt=viewed_at
        )
    ]
    owner = media_user(OWNER_ACCOUNT, "Robin")

    found = {e.item.item_id: e for e in await plex_server(plex, clock).engagement(owner)}

    assert found["100"].state == "in_progress"
    assert found["100"].progress == pytest.approx(0.25)
    assert found["200"].state == "mostly_watched"
    assert found["200"].episodes_played == 7


async def test_another_plex_user_is_reconstructed_from_the_history(
    plex: FakeInternet, clock: FakeClock
) -> None:
    friend = media_user("77", "Sam")
    viewed_at = int((clock.now() - timedelta(days=10)).timestamp())
    plex.plex_history = [
        {"type": "movie", "ratingKey": "100", "accountID": 77, "viewedAt": viewed_at},
        *(
            {
                "type": "episode",
                "ratingKey": f"20{number}",
                "grandparentRatingKey": "200",
                "accountID": 77,
                "viewedAt": viewed_at,
            }
            for number in range(7)
        ),
        # Somebody else's viewing, which the account filter must keep out.
        {"type": "movie", "ratingKey": "999", "accountID": 12, "viewedAt": viewed_at},
    ]

    found = {e.item.item_id: e for e in await plex_server(plex, clock).engagement(friend)}

    assert found["100"].state == "watched"
    assert found["200"].state == "mostly_watched"
    assert found["200"].episodes_played == 7
    history = next(r for r in plex.plex_requests if r.url.path == "/status/sessions/history/all")
    assert history.url.params.get("accountID") == "77"


async def test_the_same_episode_watched_twice_counts_once(
    plex: FakeInternet, clock: FakeClock
) -> None:
    friend = media_user("77", "Sam")
    viewed_at = int((clock.now() - timedelta(days=200)).timestamp())
    plex.plex_history = [
        {
            "type": "episode",
            "ratingKey": "201",
            "grandparentRatingKey": "200",
            "accountID": 77,
            "viewedAt": viewed_at,
        }
        for _ in range(9)
    ]

    found = {e.item.item_id: e for e in await plex_server(plex, clock).engagement(friend)}

    assert found["200"].episodes_played == 1
    assert found["200"].state == "abandoned"


async def test_a_plex_server_that_refuses_the_token_is_unreachable(
    plex: FakeInternet, clock: FakeClock
) -> None:
    connection = MediaServerConnection(
        kind="plex", url=PLEX_SERVER_URL, secret="stale", verify_tls=True, device_id="install-1"
    )
    server = PlexServer(connection, PlexTvClient(plex.plex_tv.transport), plex.transport, clock)

    with pytest.raises(ProblemError) as failure:
        await server.library_ids()
    assert failure.value.code == "media_server_unreachable"
