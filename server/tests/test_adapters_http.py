"""The shared HTTP client: what a bounded read must give back.

These tests compress for real. The adapters' own tests answer through a mock transport
that never sets ``Content-Encoding``, which is precisely why a decoding bug could reach
a real TMDb call unnoticed.
"""

import gzip
import zlib

import httpx2
import pytest

from tindarr.adapters.http import MAX_RESPONSE_BYTES, HttpSession, RemoteCallError

BODY = b'{"images": {"secure_base_url": "https://image.tmdb.org/t/p/"}}'


def transport(content: bytes, headers: dict[str, str]) -> httpx2.MockTransport:
    """A transport answering one prepared response."""
    return httpx2.MockTransport(
        lambda _request: httpx2.Response(200, headers=headers, content=content)
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "headers"),
    [
        (BODY, {}),
        (gzip.compress(BODY), {"Content-Encoding": "gzip"}),
        (zlib.compress(BODY), {"Content-Encoding": "deflate"}),
    ],
    ids=["plain", "gzip", "deflate"],
)
async def test_a_bounded_read_returns_a_body_the_caller_can_parse(
    content: bytes, headers: dict[str, str]
) -> None:
    """Whatever the wire encoding, the answer handed back is plain and parseable."""
    async with HttpSession("https://example.test", transport=transport(content, headers)) as client:
        response = await client.get_bounded("/configuration")

    assert response.json()["images"]["secure_base_url"].startswith("https://")
    assert response.text == BODY.decode()
    # The rebuilt response must not claim an encoding its body no longer has.
    assert "content-encoding" not in response.headers


@pytest.mark.anyio
async def test_a_bounded_read_keeps_the_headers_that_still_describe_the_answer() -> None:
    """Only the framing headers are dropped; the rest of the answer is untouched."""
    headers = {"Content-Encoding": "gzip", "Content-Type": "application/json", "X-Trace": "kept"}
    async with HttpSession(transport=transport(gzip.compress(BODY), headers)) as client:
        response = await client.get_bounded("https://example.test/configuration")

    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-trace"] == "kept"


@pytest.mark.anyio
async def test_a_body_larger_than_the_cap_is_refused_while_it_is_read() -> None:
    """The cap counts decoded bytes, and it stops the read rather than the parsing."""
    oversized = b"x" * (MAX_RESPONSE_BYTES + 1)
    async with HttpSession(transport=transport(oversized, {})) as client:
        with pytest.raises(RemoteCallError) as raised:
            await client.get_bounded("https://example.test/big")

    assert raised.value.reason == "oversized_response"


@pytest.mark.anyio
async def test_a_compressed_body_is_capped_on_what_it_becomes_not_on_what_it_weighs() -> None:
    """A small compressed answer that unpacks into a large one is still refused."""
    bomb = gzip.compress(b"0" * (MAX_RESPONSE_BYTES + 1))
    assert len(bomb) < MAX_RESPONSE_BYTES
    async with HttpSession(transport=transport(bomb, {"Content-Encoding": "gzip"})) as client:
        with pytest.raises(RemoteCallError) as raised:
            await client.get_bounded("https://example.test/bomb")

    assert raised.value.reason == "oversized_response"
