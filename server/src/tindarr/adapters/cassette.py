"""Recorded provider answers, so the evaluation harness costs nothing to run.

The harness has to be able to run on every pull request, which rules out calling TMDb
and an AI provider for real. It also has to measure the code that will actually ship,
which rules out replacing the adapters with hand-written fakes: a fake that parses
nothing proves nothing about the parser. A cassette is the middle: the **real** adapter,
over a transport that answers from a file recorded earlier.

- **Replay never reaches the network.** A request the cassette does not hold raises
  ``CassetteMissError`` rather than falling through, so an offline run cannot quietly become
  a paid one. The failure names the key that was missing, which is also how a fixture
  gets extended.
- **Nothing secret is written down.** The key an adapter sends — ``api_key`` in the
  query string, ``Authorization`` in the headers — is stripped before a request is
  turned into a key, and no request header is ever recorded. A cassette is therefore
  safe to commit, and a recorded cassette does not become invalid when the key rotates.
- **The file is canonical.** Interactions are sorted and the JSON is written with
  sorted keys and a trailing newline, so re-recording the same run produces the same
  bytes and a diff shows what actually changed.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Self, cast
from urllib.parse import parse_qsl, urlencode

import httpx2

from tindarr.adapters.http import decoded_response

__all__ = [
    "Cassette",
    "CassetteFormatError",
    "CassetteMissError",
    "Interaction",
    "RecordingTransport",
    "TopUpTransport",
    "request_key",
]

#: Query parameters that carry a credential. They are dropped from a key and never
#: written to a file. The list is deliberately generous: a name that is not a secret
#: costs a slightly coarser key, a secret that is not on the list costs a leak.
SECRET_PARAMS: Final[frozenset[str]] = frozenset(
    {"api_key", "apikey", "key", "token", "access_token", "secret", "session_id", "x-plex-token"}
)
#: How much of a request body goes into its key. A hash, so a recorded prompt is not
#: duplicated in the key, and a long one does not make the file unreadable.
_BODY_DIGEST_CHARS: Final = 16
_FORMAT_VERSION: Final = 1


class CassetteMissError(RuntimeError):
    """The cassette has no answer for this request, and replay refuses to invent one.

    Deliberately not a ``RemoteCallError``: an adapter turns those into "the service is
    unreachable", which is exactly the wrong story here. A miss is a missing fixture,
    and it must stop the run.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"no recorded answer for {key}")
        self.key = key


class CassetteFormatError(ValueError):
    """The file is not a cassette this version can read."""


def request_key(request: httpx2.Request) -> str:
    """Return the stable key of a request: method, address, and what varies with it.

    The query is sorted and stripped of credentials, so the same call recorded with one
    key replays with another. A body, when there is one, is reduced to a short digest:
    two different prompts must not share an answer, and a whole prompt does not belong
    in a lookup key.
    """
    url = request.url
    query = sorted(
        (name, value)
        for name, value in parse_qsl(url.query.decode(), keep_blank_values=True)
        if name.lower() not in SECRET_PARAMS
    )
    address = f"{url.scheme}://{url.host}{url.path}"
    if query:
        address = f"{address}?{urlencode(query)}"
    key = f"{request.method.upper()} {address}"
    body = request.content
    if body:
        digest = hashlib.sha256(body).hexdigest()[:_BODY_DIGEST_CHARS]
        key = f"{key} body:{digest}"
    return key


@dataclass(frozen=True, slots=True, order=True)
class Interaction:
    """One recorded answer. Ordered so a cassette file has one canonical layout."""

    key: str
    status: int
    #: The body exactly as it came back, as text. JSON in practice; kept as text so a
    #: malformed answer can be recorded too, which is the interesting failure path.
    body: str
    content_type: str = "application/json"

    def response(self) -> httpx2.Response:
        """Return this interaction as a response the adapter can read."""
        return httpx2.Response(
            self.status,
            headers={"Content-Type": self.content_type},
            content=self.body.encode(),
        )


class Cassette:
    """A set of recorded answers, addressed by ``request_key``."""

    def __init__(self, interactions: Iterable[Interaction] = (), provider: str = "") -> None:
        self.provider = provider
        self.interactions: tuple[Interaction, ...] = tuple(sorted(set(interactions)))
        by_key: dict[str, Interaction] = {}
        for interaction in self.interactions:
            # First wins, and the set above already removed exact duplicates: what is
            # left is one address recorded twice with different answers, where taking
            # the first keeps replay deterministic.
            by_key.setdefault(interaction.key, interaction)
        self._by_key: Mapping[str, Interaction] = by_key

    def __len__(self) -> int:
        """How many distinct answers the cassette holds."""
        return len(self._by_key)

    def find(self, key: str) -> Interaction | None:
        """Return the recorded answer for ``key``, or ``None``."""
        return self._by_key.get(key)

    def transport(self) -> httpx2.MockTransport:
        """Return a transport that answers from this cassette and never dials out."""

        def handle(request: httpx2.Request) -> httpx2.Response:
            key = request_key(request)
            interaction = self.find(key)
            if interaction is None:
                raise CassetteMissError(key)
            return interaction.response()

        return httpx2.MockTransport(handle)

    def dump(self) -> str:
        """Return the canonical JSON text of this cassette, newline-terminated."""
        payload = {
            "version": _FORMAT_VERSION,
            "provider": self.provider,
            "interactions": [
                {
                    "key": interaction.key,
                    "status": interaction.status,
                    "content_type": interaction.content_type,
                    "body": interaction.body,
                }
                for interaction in self.interactions
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    def write(self, path: Path) -> None:
        """Write the cassette to ``path``, creating the directory if it is missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.dump(), encoding="utf-8")

    @classmethod
    def loads(cls, text: str) -> Self:
        """Parse a cassette, refusing a file that is not one."""
        try:
            payload = cast("object", json.loads(text))
        except json.JSONDecodeError as failure:
            raise CassetteFormatError(f"the cassette is not JSON: {failure.msg}") from None
        document = _as_mapping(payload)
        if document is None:
            raise CassetteFormatError("a cassette is a JSON object")
        version = document.get("version")
        if version != _FORMAT_VERSION:
            raise CassetteFormatError(f"unsupported cassette version {version!r}")
        rows = _as_sequence(document.get("interactions"))
        if rows is None:
            raise CassetteFormatError("a cassette's 'interactions' is an array")
        provider = document.get("provider")
        return cls(
            (_interaction_of(row) for row in rows),
            provider=provider if isinstance(provider, str) else "",
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """Read a cassette from ``path``."""
        return cls.loads(path.read_text(encoding="utf-8"))


def _as_mapping(value: object) -> Mapping[str, Any] | None:
    return cast("Mapping[str, Any]", value) if isinstance(value, dict) else None


def _as_sequence(value: object) -> Sequence[object] | None:
    return cast("Sequence[object]", value) if isinstance(value, list) else None


def _as_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_status(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _interaction_of(row: object) -> Interaction:
    entry = _as_mapping(row)
    if entry is None:
        raise CassetteFormatError("a cassette interaction is a JSON object")
    key = _as_text(entry.get("key"))
    if not key:
        raise CassetteFormatError("a cassette interaction needs a 'key'")
    status = _as_status(entry.get("status"))
    if status is None:
        raise CassetteFormatError(f"the status of {key} is not a number")
    body = _as_text(entry.get("body"))
    if body is None:
        raise CassetteFormatError(f"the body of {key} is not text")
    content_type = _as_text(entry.get("content_type", "application/json"))
    if content_type is None:
        raise CassetteFormatError(f"the content type of {key} is not text")
    return Interaction(key=key, status=status, body=body, content_type=content_type)


class TopUpTransport(httpx2.AsyncBaseTransport):
    """Answers from a cassette where it can, and only dials out for what is missing.

    What a "live" run is for is **extending** a fixture, not replacing it. Without this,
    recording one strategy and then another gives the second one fresher answers than
    the first was measured on — TMDb's "most popular" moves hour to hour — and the first
    strategy's recorded model answers stop matching the prompt an offline replay builds,
    because the pool underneath them changed. The fixture then cannot be replayed at all.

    So a live run reads the committed answers first. It sees exactly what the offline
    replay will see for everything already recorded, adds only what is new, and costs
    the provider only the requests nobody has made before. Deleting the cassette is how
    a recording starts from nothing.
    """

    def __init__(self, cassette: Cassette, inner: httpx2.AsyncBaseTransport) -> None:
        self._cassette = cassette
        self._inner = inner

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Return the recorded answer, or make the call for real."""
        found = self._cassette.find(request_key(request))
        return (
            found.response()
            if found is not None
            else await self._inner.handle_async_request(request)
        )

    async def aclose(self) -> None:
        """Close the transport underneath."""
        await self._inner.aclose()


class RecordingTransport(httpx2.AsyncBaseTransport):
    """Passes calls through to a real transport and keeps what came back.

    Only ever used behind the harness's ``--live`` flag, which says out loud which
    provider it is about to call. The recorded interactions are handed back as a
    ``Cassette``; nothing is written to disk from here.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, provider: str = "") -> None:
        self._inner = inner
        self._provider = provider
        self._recorded: list[Interaction] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        """Make the call for real, record the answer, and hand it on decoded.

        ``aread`` decompresses, so what is recorded is the readable JSON a reviewer can
        diff — and what is handed back must no longer claim to be compressed, or the
        client above decodes it a second time and the whole live run dies as "the
        service did not answer" (``decoded_response``).
        """
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        self._recorded.append(
            Interaction(
                key=request_key(request),
                status=response.status_code,
                body=body.decode("utf-8", errors="replace"),
                content_type=response.headers.get("Content-Type", "application/json"),
            )
        )
        return decoded_response(response, body)

    async def aclose(self) -> None:
        """Close the transport underneath."""
        await self._inner.aclose()

    @property
    def cassette(self) -> Cassette:
        """What has been recorded so far."""
        return Cassette(self._recorded, provider=self._provider)
