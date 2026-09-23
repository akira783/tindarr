"""Every external service the optional connectors talk to, behind one transport.

``FakeInternet`` does this for the media servers and plex.tv; this is its counterpart
for step 3. One ``httpx2`` transport routes by host, so the application can be wired
with the **real** adapters and still touch nothing, and a test that points a connector
at an unknown address really points it at an unknown address.

Gemini needs a second transport: ``google-genai`` carries its own copy of ``httpx``.
"""

from dataclasses import dataclass, field
from typing import Final

import httpx
import httpx2

from tests.support.ai import FakeAnthropic, FakeGemini, FakeOllama, FakeOpenAi
from tests.support.metadata import FakeOmdb, FakeTmdb
from tests.support.requests_backend import FakeSeerr
from tindarr.adapters.factory import connector_factories
from tindarr.ports.factories import ConnectorFactories

#: Where each service answers. Only these hosts exist.
TMDB_HOST: Final = "api.themoviedb.org"
OMDB_HOST: Final = "www.omdbapi.com"
SEERR_HOST: Final = "seerr.lan"
OPENAI_HOST: Final = "api.openai.com"
MISTRAL_HOST: Final = "api.mistral.ai"
ANTHROPIC_HOST: Final = "api.anthropic.com"
COMPATIBLE_HOST: Final = "gateway.lan"
OLLAMA_HOST: Final = "ollama.lan"


@dataclass
class FakeOutside:
    """TMDb, OMDb, the request backend and the AI providers, all reachable at once."""

    tmdb: FakeTmdb = field(default_factory=FakeTmdb)
    omdb: FakeOmdb = field(default_factory=FakeOmdb)
    seerr: FakeSeerr = field(default_factory=FakeSeerr)
    openai: FakeOpenAi = field(default_factory=FakeOpenAi)
    anthropic: FakeAnthropic = field(default_factory=FakeAnthropic)
    gemini: FakeGemini = field(default_factory=FakeGemini)
    ollama: FakeOllama = field(default_factory=FakeOllama)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        """Route a request to whichever service answers at its host."""
        host = request.url.host
        if host == TMDB_HOST:
            return self.tmdb.handle(request)
        if host == OMDB_HOST:
            return self.omdb.handle(request)
        if host == SEERR_HOST:
            return self.seerr.handle(request)
        if host in (OPENAI_HOST, MISTRAL_HOST, COMPATIBLE_HOST):
            return self.openai.handle(request)
        if host == ANTHROPIC_HOST:
            return self.anthropic.handle(request)
        if host == OLLAMA_HOST:
            return self.ollama.handle(request)
        raise httpx2.ConnectError(f"nothing answers at {host}")

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport every adapter but Gemini's talks through."""
        return httpx2.MockTransport(self.handle)

    @property
    def gemini_transport(self) -> httpx.MockTransport:
        """The transport ``google-genai`` talks through."""
        return self.gemini.transport

    @property
    def factories(self) -> ConnectorFactories:
        """The real factories, wired to these fakes."""
        return connector_factories(self.transport, self.gemini_transport)
