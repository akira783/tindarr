"""Building the adapter for a stored connector: the one place that maps kind to class.

``tindarr.main`` hands the result to ``MediaServerConnector`` as a
``MediaServerFactory``; nothing else in the server knows which class answers for which
kind. The ``transport`` argument exists so the tests can run the real adapters against
fake servers (``httpx2.MockTransport``) with no network and no container.
"""

import httpx
import httpx2

from tindarr.adapters.emby import EmbyServer
from tindarr.adapters.jellyfin import JellyfinServer
from tindarr.adapters.llm.factory import llm_provider_factory
from tindarr.adapters.omdb import OmdbRatings
from tindarr.adapters.plex import PlexServer
from tindarr.adapters.plextv import PlexTvClient
from tindarr.adapters.seerr import SeerrBackend
from tindarr.adapters.tmdb import TmdbMetadata
from tindarr.core.clock import Clock, SystemClock
from tindarr.ports.factories import ConnectorFactories
from tindarr.ports.media_server import MediaServer, MediaServerConnection, MediaServerFactory
from tindarr.ports.metadata import Metadata, MetadataFactory, RatingsFactory, RatingsSource
from tindarr.ports.plextv import PlexTv
from tindarr.ports.request_backend import (
    RequestBackend,
    RequestBackendConnection,
    RequestBackendFactory,
)


def media_server_factory(
    plex_tv: PlexTv | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> MediaServerFactory:
    """Return the factory that builds an adapter for a connection.

    ``clock`` is what dates a user's watch history against: engagement turns "last
    played" into "how long ago", so a test that moves its own clock sees the states it
    set up rather than the ones today happens to produce.
    """
    client = plex_tv or PlexTvClient(transport)
    ticking = clock or SystemClock()

    def build(connection: MediaServerConnection) -> MediaServer:
        if connection.kind == "jellyfin":
            return JellyfinServer(connection, transport, ticking)
        if connection.kind == "emby":
            return EmbyServer(connection, transport, ticking)
        return PlexServer(connection, client, transport, ticking)

    return build


def metadata_factory(transport: httpx2.AsyncBaseTransport | None = None) -> MetadataFactory:
    """Return the factory that builds the TMDb adapter for a stored key."""

    def build(api_key: str) -> Metadata:
        return TmdbMetadata(api_key, transport)

    return build


def ratings_factory(transport: httpx2.AsyncBaseTransport | None = None) -> RatingsFactory:
    """Return the factory that builds the OMDb adapter for a stored key."""

    def build(api_key: str) -> RatingsSource:
        return OmdbRatings(api_key, transport)

    return build


def request_backend_factory(
    transport: httpx2.AsyncBaseTransport | None = None,
) -> RequestBackendFactory:
    """Return the factory that builds the Seerr adapter for a connection."""

    def build(connection: RequestBackendConnection) -> RequestBackend:
        return SeerrBackend(
            connection.url,
            connection.api_key,
            connection.media_server_kind,
            tv_seasons=connection.tv_seasons,
            verify_tls=connection.verify_tls,
            transport=transport,
        )

    return build


def connector_factories(
    transport: httpx2.AsyncBaseTransport | None = None,
    gemini_transport: httpx.AsyncBaseTransport | None = None,
) -> ConnectorFactories:
    """Build every optional connector's factory, for ``tindarr.connectors``.

    ``gemini_transport`` is separate because ``google-genai`` carries its own copy of
    ``httpx``; in production both are ``None``.
    """
    return ConnectorFactories(
        metadata=metadata_factory(transport),
        ratings=ratings_factory(transport),
        request_backend=request_backend_factory(transport),
        llm=llm_provider_factory(transport, gemini_transport),
    )
