"""TMDb and OMDb, driven against recorded answers (roadmap step 3).

No key, no network, no cost. What these tests hold in place is the behaviour ported from
the fork — the year retry, the empty-translation fallback, the trailer ranking, where
OMDb hides its three numbers — and the failure paths that a service one does not control
will eventually produce.
"""

from typing import Any

import pytest

from tests.support.metadata import (
    OMDB_API_KEY,
    TMDB_API_KEY,
    TMDB_BEARER,
    FakeOmdb,
    FakeTmdb,
    movie_result,
    omdb_title,
    tv_result,
)
from tindarr.adapters.omdb import OmdbRatings, ratings_of
from tindarr.adapters.tmdb import TmdbMetadata, normalize_title, passes
from tindarr.core.errors import ProblemError
from tindarr.ports.metadata import SearchQuery, Title, TitleFilters
from tindarr.ports.titles import TitleRef
from tindarr.swipe.retrieval import passes_filters

pytestmark = pytest.mark.anyio

ARRIVAL = TitleRef("movie", 329865)
SEVERANCE = TitleRef("tv", 95396)


@pytest.fixture
def tmdb() -> FakeTmdb:
    """A fake TMDb with one film and one series."""
    fake = FakeTmdb()
    fake.add_search("movie", "arrival", movie_result(329865, "Arrival"))
    fake.add_search("tv", "severance", tv_result(95396, "Severance"))
    fake.add_details(
        "movie",
        329865,
        {
            "id": 329865,
            "title": "Arrival",
            "original_title": "Arrival",
            "release_date": "2016-11-11",
            "overview": "Twelve ships arrive.",
            "tagline": "Why are they here?",
            "genres": [{"id": 878, "name": "Science Fiction"}],
            "runtime": 116,
            "poster_path": "/a.jpg",
            "backdrop_path": "/b.jpg",
            "original_language": "en",
            "vote_average": 7.6,
            "vote_count": 19000,
            "adult": False,
            "imdb_id": "tt2543164",
        },
    )
    return fake


def metadata(fake: FakeTmdb, api_key: str = TMDB_API_KEY) -> TmdbMetadata:
    """A real TMDb adapter talking to ``fake``."""
    return TmdbMetadata(api_key, fake.transport)


# --- the connection test ------------------------------------------------------------


async def test_a_good_key_tests_ok(tmdb: FakeTmdb) -> None:
    check = await metadata(tmdb).test()
    assert check.ok
    assert check.server_name == "TMDb"


async def test_a_wrong_key_is_unauthorized(tmdb: FakeTmdb) -> None:
    assert (await metadata(tmdb, "wrong").test()).health == "unauthorized"


async def test_an_unreachable_tmdb_is_unreachable(tmdb: FakeTmdb) -> None:
    tmdb.offline = True
    assert (await metadata(tmdb).test()).health == "unreachable"


async def test_a_body_that_is_not_json_is_unexpected(tmdb: FakeTmdb) -> None:
    tmdb.garbage = True
    assert (await metadata(tmdb).test()).health == "unexpected_response"


async def test_a_server_error_is_unexpected(tmdb: FakeTmdb) -> None:
    tmdb.fails["/configuration"] = 500
    assert (await metadata(tmdb).test()).health == "unexpected_response"


async def test_a_read_access_token_travels_as_a_header_not_a_query(tmdb: FakeTmdb) -> None:
    assert (await metadata(tmdb, TMDB_BEARER).test()).ok
    sent = tmdb.requests[-1]
    assert sent.headers["authorization"] == f"Bearer {TMDB_BEARER}"
    assert "api_key" not in sent.url.query.decode()


# --- search and matching ------------------------------------------------------------


async def test_a_search_uses_the_year_parameter_of_its_kind(tmdb: FakeTmdb) -> None:
    await metadata(tmdb).search(SearchQuery(title="Arrival", kind="movie", year=2016))
    assert tmdb.requests[-1].url.params.get("year") == "2016"

    await metadata(tmdb).search(SearchQuery(title="Severance", kind="tv", year=2022))
    assert tmdb.requests[-1].url.params.get("first_air_date_year") == "2022"


async def test_a_search_that_finds_nothing_is_retried_without_the_year(
    tmdb: FakeTmdb,
) -> None:
    found = await metadata(tmdb).search(SearchQuery(title="Arrival", kind="movie", year=2015))

    assert [title.ref for title in found] == [ARRIVAL]
    years = [request.url.params.get("year") for request in tmdb.requests]
    assert years == ["2015", None]


async def test_an_alias_is_tried_when_nothing_else_matched(tmdb: FakeTmdb) -> None:
    tmdb.add_search("movie", "premier contact", movie_result(329865, "Arrival"))

    found = await metadata(tmdb).search(
        SearchQuery(title="Arrival 2", kind="movie", aliases=("Premier Contact",))
    )

    assert [title.ref for title in found] == [ARRIVAL]


async def test_the_right_year_beats_the_more_popular_near_miss(tmdb: FakeTmdb) -> None:
    tmdb.add_search(
        "movie",
        "dune",
        movie_result(841, "Dune", 1984, popularity=90.0),
        movie_result(438631, "Dune", 2021, popularity=30.0),
    )

    found = await metadata(tmdb).match(SearchQuery(title="Dune", kind="movie", year=2021))

    assert found is not None
    assert found.ref.tmdb_id == 438631


async def test_an_exact_title_beats_a_more_popular_one(tmdb: FakeTmdb) -> None:
    tmdb.add_search(
        "movie",
        "arrival",
        movie_result(1, "The Arrival of Everything", 2016, popularity=99.0),
        movie_result(329865, "Arrival", 2016, popularity=1.0),
    )

    found = await metadata(tmdb).match(SearchQuery(title="Arrival", kind="movie"))

    assert found is not None
    assert found.ref == ARRIVAL


async def test_with_nothing_to_choose_between_tmdbs_own_order_wins(tmdb: FakeTmdb) -> None:
    tmdb.add_search(
        "movie",
        "unknown",
        movie_result(1, "First Guess", 2016, popularity=5.0),
        movie_result(2, "Second Guess", 2016, popularity=5.0),
    )

    found = await metadata(tmdb).match(SearchQuery(title="Unknown", kind="movie"))

    assert found is not None
    assert found.ref.tmdb_id == 1


async def test_a_suggestion_nothing_matches_is_no_match(tmdb: FakeTmdb) -> None:
    assert await metadata(tmdb).match(SearchQuery(title="Nothing", kind="movie")) is None


async def test_a_result_without_an_id_is_dropped(tmdb: FakeTmdb) -> None:
    tmdb.searches[("movie", "broken")] = [{"title": "No id"}, movie_result(7, "Fine")]

    found = await metadata(tmdb).search(SearchQuery(title="Broken", kind="movie"))

    assert [title.ref.tmdb_id for title in found] == [7]


async def test_a_search_that_is_not_a_list_answers_nothing(tmdb: FakeTmdb) -> None:
    tmdb.searches[("movie", "odd")] = []
    found = await metadata(tmdb).search(SearchQuery(title="Odd", kind="movie"))
    assert found == []


async def test_a_failing_search_is_a_metadata_problem(tmdb: FakeTmdb) -> None:
    tmdb.fails["/search/movie"] = 429

    with pytest.raises(ProblemError) as failure:
        await metadata(tmdb).search(SearchQuery(title="Arrival", kind="movie"))
    assert failure.value.code == "metadata_unreachable"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Amélie", "Amelie"),
        ("Spider-Man: No Way Home", "spider man no way home"),
        ("Dune (2021)", "Dune"),
        ("  The   Thing  ", "the thing"),
    ],
)
def test_titles_are_compared_on_what_two_spellings_share(left: str, right: str) -> None:
    assert normalize_title(left) == normalize_title(right)


def test_a_bare_year_is_not_mistaken_for_a_decoration() -> None:
    assert normalize_title("2012") == "2012"


# --- content filters ----------------------------------------------------------------


def a_title(**fields: Any) -> Title:
    """A candidate to run the filters against."""
    defaults: dict[str, Any] = {
        "ref": ARRIVAL,
        "title": "Arrival",
        "year": 2016,
        "original_language": "en",
        "genre_ids": (878,),
        "adult": False,
    }
    return Title(**(defaults | fields))


def test_the_content_filters() -> None:
    strict = TitleFilters(exclude_adult=True, min_year=2000)
    assert passes(a_title(), strict, frozenset())
    assert not passes(a_title(adult=True), strict, frozenset())
    assert not passes(a_title(year=1975), strict, frozenset())
    # A title TMDb has no year for is not refused for lacking one.
    assert passes(a_title(year=None), strict, frozenset())
    assert not passes(a_title(), strict, frozenset({878}))
    languages = TitleFilters(excluded_original_languages=frozenset({"ja"}))
    assert not passes(a_title(original_language="JA"), languages, frozenset())
    assert passes(a_title(original_language=None), languages, frozenset())


async def test_genre_names_are_resolved_against_tmdbs_own_list(tmdb: FakeTmdb) -> None:
    adapter = metadata(tmdb)
    filters = TitleFilters(excluded_genres=frozenset({"horror", "Drama", "10402"}))

    assert await adapter.excluded_genre_ids(filters) == {27, 18, 10402}
    # Read once per adapter, whatever the caller asks again.
    calls = len([r for r in tmdb.requests if r.url.path.startswith("/3/genre")])
    await adapter.excluded_genre_ids(filters)
    assert len([r for r in tmdb.requests if r.url.path.startswith("/3/genre")]) == calls


async def test_a_genre_nobody_recognises_filters_nothing(tmdb: FakeTmdb) -> None:
    found = await metadata(tmdb).excluded_genre_ids(
        TitleFilters(excluded_genres=frozenset({"Documentaries"}))
    )
    assert found == frozenset()


async def test_no_excluded_genres_costs_no_call(tmdb: FakeTmdb) -> None:
    assert await metadata(tmdb).excluded_genre_ids(TitleFilters()) == frozenset()
    assert tmdb.requests == []


async def test_a_match_applies_the_filters(tmdb: FakeTmdb) -> None:
    tmdb.add_search(
        "movie",
        "arrival",
        movie_result(1, "Arrival", 2016, genre_ids=[27]),
        movie_result(329865, "Arrival", 2016, genre_ids=[878]),
    )

    found = await metadata(tmdb).match(
        SearchQuery(
            title="Arrival",
            kind="movie",
            filters=TitleFilters(excluded_genres=frozenset({"Horror"})),
        )
    )

    assert found is not None
    assert found.ref == ARRIVAL


# --- details ------------------------------------------------------------------------


async def test_details_are_read_in_the_language_asked_for(tmdb: FakeTmdb) -> None:
    tmdb.add_details(
        "movie",
        329865,
        {
            "id": 329865,
            "title": "Premier Contact",
            "original_title": "Arrival",
            "release_date": "2016-11-11",
            "overview": "Douze vaisseaux arrivent.",
            "genres": [{"id": 878, "name": "Science-fiction"}],
            "runtime": 116,
            "imdb_id": "tt2543164",
        },
        language="fr",
    )

    found = await metadata(tmdb).details(ARRIVAL, "fr")

    assert found.title == "Premier Contact"
    assert found.overview == "Douze vaisseaux arrivent."
    assert found.genres == ("Science-fiction",)
    assert found.imdb_id == "tt2543164"
    assert found.year == 2016
    assert found.runtime_minutes == 116


async def test_an_empty_translation_keeps_the_english_prose(tmdb: FakeTmdb) -> None:
    tmdb.add_details(
        "movie",
        329865,
        {
            "id": 329865,
            "title": "Premier Contact",
            "original_title": "Arrival",
            "release_date": "2016-11-11",
            "overview": "",
            "tagline": "",
            "runtime": 116,
        },
        language="fr",
    )

    found = await metadata(tmdb).details(ARRIVAL, "fr")

    assert found.title == "Premier Contact"
    assert found.overview == "Twelve ships arrive."
    assert found.tagline == "Why are they here?"


async def test_english_details_cost_one_call(tmdb: FakeTmdb) -> None:
    await metadata(tmdb).details(ARRIVAL, "en")
    assert len([r for r in tmdb.requests if r.url.path == "/3/movie/329865"]) == 1


async def test_a_series_takes_its_imdb_id_from_its_external_ids(tmdb: FakeTmdb) -> None:
    tmdb.add_details(
        "tv",
        95396,
        {
            "id": 95396,
            "name": "Severance",
            "first_air_date": "2022-02-18",
            "overview": "Work-life balance.",
            "episode_run_time": [55],
            "number_of_seasons": 2,
            "number_of_episodes": 19,
            "external_ids": {"imdb_id": "tt11280740"},
        },
    )

    found = await metadata(tmdb).details(SEVERANCE, "en")

    assert found.imdb_id == "tt11280740"
    assert found.runtime_minutes == 55
    assert found.seasons == 2
    assert found.episodes == 19
    assert tmdb.requests[-1].url.params.get("append_to_response") == "external_ids"


async def test_an_imdb_id_that_is_not_one_is_dropped(tmdb: FakeTmdb) -> None:
    tmdb.add_details("movie", 42, {"id": 42, "title": "Odd", "imdb_id": "0000"})
    found = await metadata(tmdb).details(TitleRef("movie", 42), "en")
    assert found.imdb_id is None


async def test_a_title_tmdb_lost_is_a_metadata_problem(tmdb: FakeTmdb) -> None:
    with pytest.raises(ProblemError) as failure:
        await metadata(tmdb).details(TitleRef("movie", 999), "en")
    assert failure.value.code == "metadata_unreachable"


# --- watch providers ----------------------------------------------------------------


async def test_watch_providers_are_read_for_one_region_only(tmdb: FakeTmdb) -> None:
    tmdb.providers[("movie", 329865)] = {
        "FR": {
            "flatrate": [
                {
                    "provider_id": 8,
                    "provider_name": "Netflix",
                    "logo_path": "/n.jpg",
                    "display_priority": 2,
                },
            ],
            "ads": [
                {"provider_id": 613, "provider_name": "Free", "display_priority": 1},
            ],
            "rent": [
                {"provider_id": 8, "provider_name": "Netflix", "display_priority": 9},
                {"provider_id": 2, "provider_name": "Apple TV", "display_priority": 5},
            ],
        },
        "US": {"flatrate": [{"provider_id": 15, "provider_name": "Hulu"}]},
    }

    found = await metadata(tmdb).watch_providers(ARRIVAL, "fr")

    assert [(p.provider_id, p.offer) for p in found] == [
        (8, "subscription"),
        (613, "ads"),
        (2, "rent"),
    ]
    assert found[0].logo_path == "/n.jpg"
    assert found[0].included
    assert not found[2].included


async def test_a_region_with_no_offers_has_no_providers(tmdb: FakeTmdb) -> None:
    assert await metadata(tmdb).watch_providers(ARRIVAL, "BE") == []


async def test_the_regions_providers_are_listed_by_name(tmdb: FakeTmdb) -> None:
    tmdb.region_providers = [
        {"provider_id": 8, "provider_name": "Netflix", "logo_path": "/n.jpg"},
        {"provider_id": 2, "provider_name": "Apple TV"},
        {"provider_name": "Broken"},
    ]

    found = await metadata(tmdb).region_providers("fr")

    assert [provider.name for provider in found] == ["Apple TV", "Netflix"]
    assert tmdb.requests[-1].url.params.get("watch_region") == "FR"


async def test_failing_provider_calls_are_metadata_problems(tmdb: FakeTmdb) -> None:
    tmdb.offline = True
    with pytest.raises(ProblemError):
        await metadata(tmdb).watch_providers(ARRIVAL, "FR")
    with pytest.raises(ProblemError):
        await metadata(tmdb).region_providers("FR")


# --- trailers -----------------------------------------------------------------------


async def test_the_best_trailer_is_official_in_the_right_language(tmdb: FakeTmdb) -> None:
    tmdb.videos[("movie", 329865)] = [
        {"site": "Vimeo", "key": "nope", "type": "Trailer", "official": True, "iso_639_1": "fr"},
        {"site": "YouTube", "key": "teaser", "type": "Teaser", "official": True, "iso_639_1": "fr"},
        {"site": "YouTube", "key": "fan", "type": "Trailer", "official": False, "iso_639_1": "fr"},
        {
            "site": "YouTube",
            "key": "english",
            "type": "Trailer",
            "official": True,
            "iso_639_1": "en",
        },
        {
            "site": "YouTube",
            "key": "french",
            "type": "Trailer",
            "official": True,
            "iso_639_1": "fr",
        },
    ]

    found = await metadata(tmdb).trailer(ARRIVAL, "fr-BE")

    assert found is not None
    assert found.key == "french"
    assert tmdb.requests[-1].url.params.get("include_video_language") == "fr,en,null"


async def test_english_is_the_fallback_language(tmdb: FakeTmdb) -> None:
    tmdb.videos[("movie", 329865)] = [
        {
            "site": "YouTube",
            "key": "german",
            "type": "Trailer",
            "official": True,
            "iso_639_1": "de",
        },
        {
            "site": "YouTube",
            "key": "english",
            "type": "Trailer",
            "official": True,
            "iso_639_1": "en",
        },
    ]

    found = await metadata(tmdb).trailer(ARRIVAL, "fr")
    assert found is not None
    assert found.key == "english"


async def test_a_title_with_no_youtube_video_has_no_trailer(tmdb: FakeTmdb) -> None:
    tmdb.videos[("movie", 329865)] = [
        {"site": "YouTube", "key": "clip", "type": "Clip", "official": True},
        {"site": "YouTube", "type": "Trailer", "official": True},
    ]
    assert await metadata(tmdb).trailer(ARRIVAL, "en") is None


@pytest.mark.parametrize(
    "key", ["../../evil", "abc", "a" * 40, "key with space", "https://evil.test/v"]
)
async def test_a_video_key_that_is_not_a_youtube_id_is_dropped(tmdb: FakeTmdb, key: str) -> None:
    # The app turns this into a youtube-nocookie.com URL, so it becomes part of a link.
    tmdb.videos[("movie", 329865)] = [
        {"site": "YouTube", "key": key, "type": "Trailer", "official": True}
    ]
    assert await metadata(tmdb).trailer(ARRIVAL, "en") is None


@pytest.mark.parametrize(
    "path", ["../../etc/passwd", "https://evil.test/a.jpg", "no-leading-slash.jpg", "/"]
)
async def test_an_image_path_that_would_leave_tmdbs_host_is_dropped(
    tmdb: FakeTmdb, path: str
) -> None:
    tmdb.add_search("movie", "odd", movie_result(9, "Odd", poster_path=path))

    found = await metadata(tmdb).search(SearchQuery(title="Odd", kind="movie"))

    assert found[0].poster_path is None


async def test_a_real_image_path_is_kept(tmdb: FakeTmdb) -> None:
    tmdb.add_search("movie", "fine", movie_result(9, "Fine", poster_path="/a1b2.jpg"))
    found = await metadata(tmdb).search(SearchQuery(title="Fine", kind="movie"))
    assert found[0].poster_path == "/a1b2.jpg"


async def test_a_provider_logo_is_a_path_too(tmdb: FakeTmdb) -> None:
    tmdb.providers[("movie", 329865)] = {
        "FR": {
            "flatrate": [
                {"provider_id": 8, "provider_name": "Netflix", "logo_path": "//evil.test/x"}
            ]
        }
    }
    found = await metadata(tmdb).watch_providers(ARRIVAL, "FR")
    assert found[0].logo_path is None


async def test_a_failing_video_call_is_a_metadata_problem(tmdb: FakeTmdb) -> None:
    tmdb.fails["/movie/329865/videos"] = 503
    with pytest.raises(ProblemError):
        await metadata(tmdb).trailer(ARRIVAL, "en")


# --- OMDb ---------------------------------------------------------------------------


@pytest.fixture
def omdb() -> FakeOmdb:
    """A fake OMDb that knows the probe title and Arrival."""
    fake = FakeOmdb()
    fake.titles["tt0111261"] = omdb_title()
    fake.titles["tt0111161"] = omdb_title()
    fake.titles["tt2543164"] = omdb_title()
    return fake


def ratings(fake: FakeOmdb, api_key: str = OMDB_API_KEY) -> OmdbRatings:
    """A real OMDb adapter talking to ``fake``."""
    return OmdbRatings(api_key, fake.transport)


async def test_omdb_tests_ok_with_a_good_key(omdb: FakeOmdb) -> None:
    check = await ratings(omdb).test()
    assert check.ok
    assert check.server_name == "OMDb"
    assert omdb.requests[-1].url.params.get("i") == "tt0111161"


async def test_a_wrong_omdb_key_is_unauthorized(omdb: FakeOmdb) -> None:
    assert (await ratings(omdb, "wrong").test()).health == "unauthorized"


async def test_an_omdb_that_says_no_to_a_title_it_always_has_is_unauthorized(
    omdb: FakeOmdb,
) -> None:
    del omdb.titles["tt0111161"]
    assert (await ratings(omdb).test()).health == "unauthorized"


async def test_an_unreachable_omdb_is_unreachable(omdb: FakeOmdb) -> None:
    omdb.offline = True
    assert (await ratings(omdb).test()).health == "unreachable"


async def test_omdb_garbage_is_unexpected(omdb: FakeOmdb) -> None:
    omdb.garbage = True
    assert (await ratings(omdb).test()).health == "unexpected_response"


async def test_an_omdb_server_error_is_unexpected(omdb: FakeOmdb) -> None:
    omdb.status = 502
    assert (await ratings(omdb).test()).health == "unexpected_response"


async def test_the_three_ratings_come_from_where_omdb_really_keeps_them(
    omdb: FakeOmdb,
) -> None:
    found = await ratings(omdb).ratings("tt2543164")

    assert found is not None
    assert found.imdb == pytest.approx(7.9)
    assert found.rotten_tomatoes == 94
    assert found.metacritic == 81


@pytest.mark.parametrize("missing", ["N/A", "", "n/a"])
async def test_omdbs_ways_of_saying_nothing(omdb: FakeOmdb, missing: str) -> None:
    omdb.titles["tt2543164"] = omdb_title(imdb_rating=missing, metascore=missing, tomatoes=missing)
    assert await ratings(omdb).ratings("tt2543164") is None


async def test_a_partial_answer_keeps_what_it_has(omdb: FakeOmdb) -> None:
    omdb.titles["tt2543164"] = omdb_title(imdb_rating="N/A", tomatoes=None)

    found = await ratings(omdb).ratings("tt2543164")

    assert found is not None
    assert found.imdb is None
    assert found.rotten_tomatoes is None
    assert found.metacritic == 81


@pytest.mark.parametrize(
    ("field", "value"),
    [("imdb_rating", "eleven"), ("imdb_rating", "42"), ("metascore", "-3"), ("metascore", "x")],
)
async def test_a_number_outside_its_scale_is_no_number(
    omdb: FakeOmdb, field: str, value: str
) -> None:
    omdb.titles["tt2543164"] = omdb_title(**{field: value})
    found = await ratings(omdb).ratings("tt2543164")
    assert found is not None
    assert getattr(found, "imdb" if field == "imdb_rating" else "metacritic") is None


async def test_a_title_omdb_does_not_know_has_no_ratings(omdb: FakeOmdb) -> None:
    assert await ratings(omdb).ratings("tt9999999") is None


async def test_a_value_that_is_not_an_imdb_id_is_never_sent(omdb: FakeOmdb) -> None:
    assert await ratings(omdb).ratings("329865") is None
    assert omdb.requests == []


async def test_an_omdb_failure_costs_a_badge_not_a_batch(omdb: FakeOmdb) -> None:
    omdb.offline = True
    assert await ratings(omdb).ratings("tt2543164") is None

    omdb.offline = False
    omdb.status = 500
    assert await ratings(omdb).ratings("tt2543164") is None


def test_the_last_rotten_tomatoes_entry_wins() -> None:
    found = ratings_of(
        {
            "Response": "True",
            "Ratings": [
                {"Source": "Rotten Tomatoes", "Value": "50%"},
                {"Source": "Rotten Tomatoes", "Value": "94%"},
            ],
        }
    )
    assert found is not None
    assert found.rotten_tomatoes == 94


def test_the_domain_and_the_adapter_filter_a_title_the_same_way() -> None:
    """Two implementations of "what the household refuses" would drift, so they agree.

    The adapter applies its own when it resolves a search; the retrieval layer and the
    baselines apply ``passes_filters`` to the candidate pool. A title that survives one
    and not the other means the floors and the candidate are being handed different
    shortlists, which is exactly the confound lot 4b removed.
    """
    filters = TitleFilters(
        exclude_adult=True,
        min_year=2000,
        excluded_original_languages=frozenset({"RU"}),
    )
    excluded = frozenset({27})
    cases = [
        Title(ref=TitleRef("movie", 1), title="Plain"),
        Title(ref=TitleRef("movie", 2), title="Adult", adult=True),
        Title(ref=TitleRef("movie", 3), title="Old", year=1974),
        Title(ref=TitleRef("movie", 4), title="Recent", year=2020),
        Title(ref=TitleRef("movie", 5), title="Russian", year=2020, original_language="ru"),
        Title(ref=TitleRef("movie", 6), title="Horror", year=2020, genre_ids=(27,)),
        Title(ref=TitleRef("movie", 7), title="No year at all", original_language=None),
    ]

    assert [passes(title, filters, excluded) for title in cases] == [
        passes_filters(title, filters, excluded) for title in cases
    ]
