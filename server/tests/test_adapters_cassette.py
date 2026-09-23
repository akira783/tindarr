"""Recorded provider answers: replay never dials out, and never writes down a key."""

import gzip
import json
from pathlib import Path

import httpx2
import pytest

from tests.support.metadata import TMDB_API_KEY, TMDB_BEARER, FakeTmdb, movie_result
from tindarr.adapters.cassette import (
    Cassette,
    CassetteFormatError,
    CassetteMissError,
    Interaction,
    RecordingTransport,
    request_key,
)
from tindarr.adapters.tmdb import TmdbMetadata
from tindarr.ports.titles import TitleRef

pytestmark = pytest.mark.anyio

ARRIVAL = TitleRef("movie", 329865)


def request(url: str, method: str = "GET", content: bytes | None = None) -> httpx2.Request:
    """One outbound request, as an adapter would build it."""
    return httpx2.Request(method, url, content=content)


def test_a_key_drops_the_credential_and_sorts_the_query() -> None:
    with_key = request("https://api.themoviedb.org/3/movie/1?language=en&api_key=abcdef")
    rotated = request("https://api.themoviedb.org/3/movie/1?api_key=zzzzzz&language=en")
    assert request_key(with_key) == request_key(rotated)
    assert "abcdef" not in request_key(with_key)
    assert request_key(with_key) == "GET https://api.themoviedb.org/3/movie/1?language=en"


def test_query_order_does_not_change_a_key() -> None:
    one = request("https://x/y?b=2&a=1")
    two = request("https://x/y?a=1&b=2")
    assert request_key(one) == request_key(two)


def test_two_bodies_never_share_an_answer() -> None:
    first = request("https://x/y", "POST", content=b'{"prompt": "one"}')
    second = request("https://x/y", "POST", content=b'{"prompt": "two"}')
    assert request_key(first) != request_key(second)
    # The prompt itself is not in the key; a digest of it is.
    assert "prompt" not in request_key(first)


async def test_the_real_adapter_reads_a_cassette_and_touches_no_network() -> None:
    details = {
        "id": 329865,
        "title": "Arrival",
        "release_date": "2016-11-11",
        "overview": "Twelve ships arrive.",
        "genres": [{"id": 878, "name": "Science Fiction"}],
    }
    cassette = Cassette(
        [
            Interaction(
                key="GET https://api.themoviedb.org/3/movie/329865?language=en",
                status=200,
                body=json.dumps(details),
            )
        ],
        provider="tmdb",
    )
    tmdb = TmdbMetadata("unused-key", transport=cassette.transport())

    title = await tmdb.details(ARRIVAL, "en")

    assert title.title == "Arrival"
    assert title.genres == ("Science Fiction",)


async def test_a_request_nobody_recorded_stops_the_run() -> None:
    tmdb = TmdbMetadata("unused-key", transport=Cassette().transport())
    with pytest.raises(CassetteMissError) as failure:
        await tmdb.details(ARRIVAL, "en")
    assert "movie/329865" in failure.value.key


async def test_recording_keeps_the_answer_and_not_the_key() -> None:
    fake = FakeTmdb()
    fake.add_details("movie", 329865, movie_result(329865, "Arrival"))
    recorder = RecordingTransport(fake.transport, provider="tmdb")
    tmdb = TmdbMetadata(TMDB_API_KEY, transport=recorder)

    live = await tmdb.details(ARRIVAL, "en")
    cassette = recorder.cassette

    assert live.title == "Arrival"
    assert len(cassette) == 1
    written = cassette.dump()
    assert TMDB_API_KEY not in written
    assert TMDB_BEARER not in written
    # And the recording replays into the same answer.
    replayed = await TmdbMetadata("other-key", transport=cassette.transport()).details(
        ARRIVAL, "en"
    )
    assert replayed == live
    await recorder.aclose()


async def test_recording_a_compressed_answer_does_not_break_the_live_run() -> None:
    """The one thing a mock transport never does: answer ``Content-Encoding: gzip``.

    Every other test here answers in plain bytes, so a recorder that handed the decoded
    body back under the original headers looked perfect offline and killed every live
    run: the client decompressed a second time, ``httpx`` raised ``DecodingError``, and
    the adapter reported it as "the metadata service did not answer".
    """
    details = {"id": 329865, "title": "Arrival", "release_date": "2016-11-11"}
    body = json.dumps(details).encode()
    compressed = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200,
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            content=gzip.compress(body),
        )
    )
    recorder = RecordingTransport(compressed, provider="tmdb")

    live = await TmdbMetadata(TMDB_API_KEY, transport=recorder).details(ARRIVAL, "en")

    assert live.title == "Arrival"
    # And what was written down is the readable JSON, not the compressed bytes.
    interaction = recorder.cassette.interactions[0]
    assert json.loads(interaction.body)["title"] == "Arrival"
    replayed = await TmdbMetadata("other-key", transport=recorder.cassette.transport()).details(
        ARRIVAL, "en"
    )
    assert replayed == live
    await recorder.aclose()


def test_a_cassette_round_trips_through_its_file(tmp_path: Path) -> None:
    cassette = Cassette(
        [
            Interaction("GET https://b/2", 404, "{}"),
            Interaction("GET https://a/1", 200, '{"ok": true}'),
        ],
        provider="tmdb",
    )
    path = tmp_path / "nested" / "tmdb.json"
    cassette.write(path)
    reloaded = Cassette.load(path)

    assert reloaded.dump() == cassette.dump()
    assert [row.key for row in reloaded.interactions] == ["GET https://a/1", "GET https://b/2"]
    assert reloaded.provider == "tmdb"
    # Canonical: written once more, the bytes do not move.
    reloaded.write(path)
    assert path.read_text(encoding="utf-8") == cassette.dump()


def test_the_same_address_recorded_twice_replays_deterministically() -> None:
    cassette = Cassette(
        [Interaction("GET https://a/1", 200, "{}"), Interaction("GET https://a/1", 500, "{}")]
    )
    found = cassette.find("GET https://a/1")
    assert found is not None
    assert found.status == 200
    assert len(cassette) == 1


def test_an_exact_duplicate_is_recorded_once() -> None:
    row = Interaction("GET https://a/1", 200, "{}")
    assert len(Cassette([row, row])) == 1


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json", "not JSON"),
        ("[]", "JSON object"),
        ('{"version": 9, "interactions": []}', "unsupported cassette version"),
        ('{"version": 1, "interactions": {}}', "is an array"),
        ('{"version": 1, "interactions": ["x"]}', "is a JSON object"),
        ('{"version": 1, "interactions": [{}]}', "needs a 'key'"),
        ('{"version": 1, "interactions": [{"key": "k", "status": true}]}', "not a number"),
        ('{"version": 1, "interactions": [{"key": "k", "status": 200}]}', "not text"),
        (
            '{"version": 1, "interactions": '
            '[{"key": "k", "status": 200, "body": "", "content_type": 3}]}',
            "content type",
        ),
    ],
)
def test_a_file_that_is_not_a_cassette_is_refused(text: str, message: str) -> None:
    with pytest.raises(CassetteFormatError, match=message):
        Cassette.loads(text)


def test_a_cassette_without_a_provider_name_still_loads() -> None:
    assert Cassette.loads('{"version": 1, "interactions": [], "provider": 3}').provider == ""
