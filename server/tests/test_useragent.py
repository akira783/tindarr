"""Naming the browser a console session belongs to."""

import pytest

from tindarr.api.useragent import MAX_LENGTH, UNKNOWN, browser_name, console_device

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
EDGE = f"{CHROME} Edg/131.0.0.0"
SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (FIREFOX, "Firefox on Linux"),
        (CHROME, "Chrome on Windows"),
        # Every Chromium browser also says Chrome and Safari: the specific name wins.
        (EDGE, "Edge on Windows"),
        (SAFARI, "Safari on macOS"),
        ("Mozilla/5.0 (Linux; Android 15) Chrome/131.0", "Chrome on Android"),
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/605.1", "Safari on iPhone"),
        ("curl/8.5.0", UNKNOWN),
        ("something on Linux", f"{UNKNOWN} on Linux"),
        (None, UNKNOWN),
        ("", UNKNOWN),
    ],
)
def test_a_browser_gets_a_short_readable_name(user_agent: str | None, expected: str) -> None:
    assert browser_name(user_agent) == expected


def test_an_absurd_user_agent_is_cut_before_it_is_read() -> None:
    assert browser_name("x" * 10_000 + " Firefox/1") == UNKNOWN
    assert MAX_LENGTH < 10_000


def test_a_console_session_is_described_as_a_web_device() -> None:
    device = console_device(FIREFOX)
    assert (device.name, device.platform, device.app_version) == (
        "Firefox on Linux",
        "web",
        None,
    )
