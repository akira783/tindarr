"""Emby: the Media Browser API, without Quick Connect (docs/auth.md, section 4).

Emby is the ancestor Jellyfin forked from, so everything this step needs is in
``tindarr.adapters.mediabrowser``. Only two things are Emby's own:

- **No Quick Connect.** Emby has no equivalent, so the inherited refusals
  (``quick_connect_unavailable``) are the right answer and nothing is implemented here.
- **No version floor.** The end-to-end workflow pins one Emby image; older ones are
  best effort. A build that stops answering ``/System/Info/Public`` as Emby still fails
  the connection test.

Emby identifies a device by the four values of the ``Authorization: MediaBrowser …``
header together, so Tindarr's ``Version`` is the constant ``1`` rather than its own
version: otherwise every release would appear as a new device in the user's account.
"""

from tindarr.adapters.mediabrowser import MediaBrowserServer
from tindarr.ports.media_server import MediaServerKind


class EmbyServer(MediaBrowserServer):
    """The ``MediaServer`` port for Emby."""

    kind: MediaServerKind = "emby"
    #: Emby's web client routes items through ``index.html``, Jellyfin's does not.
    item_path: str = "/web/index.html#!/item?id={id}"
