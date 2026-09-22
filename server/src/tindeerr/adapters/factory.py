"""Building the adapter for a stored connector: the one place that maps kind to class.

``tindeerr.main`` hands the result to ``MediaServerConnector`` as a
``MediaServerFactory``; nothing else in the server knows which class answers for which
kind. The ``transport`` argument exists so the tests can run the real adapters against
fake servers (``httpx2.MockTransport``) with no network and no container.
"""

import httpx2

from tindeerr.adapters.emby import EmbyServer
from tindeerr.adapters.jellyfin import JellyfinServer
from tindeerr.adapters.plex import PlexServer
from tindeerr.adapters.plextv import PlexTvClient
from tindeerr.ports.media_server import MediaServer, MediaServerConnection, MediaServerFactory
from tindeerr.ports.plextv import PlexTv


def media_server_factory(
    plex_tv: PlexTv | None = None, transport: httpx2.AsyncBaseTransport | None = None
) -> MediaServerFactory:
    """Return the factory that builds an adapter for a connection."""
    client = plex_tv or PlexTvClient(transport)

    def build(connection: MediaServerConnection) -> MediaServer:
        if connection.kind == "jellyfin":
            return JellyfinServer(connection, transport)
        if connection.kind == "emby":
            return EmbyServer(connection, transport)
        return PlexServer(connection, client, transport)

    return build
