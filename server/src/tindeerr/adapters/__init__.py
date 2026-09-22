"""One sub-package per external system (media servers, request backends, metadata, AI).

Adapters implement ``tindeerr.ports`` and may use vendor SDKs and httpx. Nothing but
``tindeerr.main`` imports them; import-linter enforces it.

Step 2 brings the sign-in side of the media servers: ``jellyfin`` and ``emby`` over the
Media Browser API they share (``mediabrowser``), ``plex`` over the Plex server and
``plextv`` over the account service behind it. ``http`` holds the client they all use,
and ``factory`` maps a stored connector to one of them.
"""
