"""Building the adapter for a stored connector: the one place that maps kind to class.

``tindarr.main`` hands the result to ``MediaServerConnector`` as a
``MediaServerFactory``; nothing else in the server knows which class answers for which
kind. The ``transport`` argument exists so the tests can run the real adapters against
fake servers (``httpx2.MockTransport``) with no network and no container.
"""

import httpx2

from tindarr.adapters.emby import EmbyServer
from tindarr.adapters.jellyfin import JellyfinServer
from tindarr.adapters.plex import PlexServer
from tindarr.adapters.plextv import PlexTvClient
from tindarr.core.clock import Clock, SystemClock
from tindarr.ports.media_server import MediaServer, MediaServerConnection, MediaServerFactory
from tindarr.ports.plextv import PlexTv


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
