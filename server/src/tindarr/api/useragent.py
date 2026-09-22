"""A short, readable name for the browser a console session belongs to.

The app names its own device (``DeviceInput``); a browser does not, so the session list
would otherwise show nothing recognisable next to "revoke". This reads just enough of
the ``User-Agent`` to write "Firefox on Linux" — never the whole header, which is
fingerprinting material and often absurdly long.

The order matters: every Chromium browser also says ``Safari``, and Edge also says
``Chrome``, so the more specific name has to be looked for first.
"""

from typing import Final

from tindarr.storage.sessions import Device

#: Longest ``User-Agent`` looked at; anything beyond is noise or an attack.
MAX_LENGTH: Final = 512
UNKNOWN: Final = "Web browser"
#: Browser families, most specific first (Edge says Chrome, Chrome says Safari).
_BROWSERS: Final = (
    ("edg/", "Edge"),
    ("opr/", "Opera"),
    ("firefox/", "Firefox"),
    ("chrome/", "Chrome"),
    ("safari/", "Safari"),
)
_SYSTEMS: Final = (
    ("android", "Android"),
    ("iphone", "iPhone"),
    ("ipad", "iPad"),
    ("mac os", "macOS"),
    ("windows", "Windows"),
    ("cros", "ChromeOS"),
    ("linux", "Linux"),
)


def browser_name(user_agent: str | None) -> str:
    """Return something like ``Firefox on Linux``, or a generic name."""
    text = (user_agent or "")[:MAX_LENGTH].lower()
    browser = next((name for token, name in _BROWSERS if token in text), None)
    system = next((name for token, name in _SYSTEMS if token in text), None)
    if browser is None:
        return UNKNOWN if system is None else f"{UNKNOWN} on {system}"
    return browser if system is None else f"{browser} on {system}"


def console_device(user_agent: str | None) -> Device:
    """Describe the browser a ``web`` session belongs to."""
    return Device(name=browser_name(user_agent), platform="web")
