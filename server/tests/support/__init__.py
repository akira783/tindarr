"""Test doubles and helpers shared by the step-2 test modules.

``fakes`` holds a controllable clock and a fake media server implementing the port;
``console`` builds applications and clients that behave like the web console. Steps 2b
and 2c build their own fakes on these.
"""

from tests.support.console import (
    CONSOLE_HOST,
    CONSOLE_ORIGIN,
    CONSOLE_PEER,
    TEST_HOST,
    build_app,
    claim,
    complete_setup,
    configure_media_server,
    console_client,
    console_headers,
    cookies_of,
    run,
    server_config,
    services_of,
    setup_code,
    sign_in_console,
)
from tests.support.fakes import FakeClock, FakeMediaServer, FakeMediaServers, media_user

__all__ = [
    "CONSOLE_HOST",
    "CONSOLE_ORIGIN",
    "CONSOLE_PEER",
    "TEST_HOST",
    "FakeClock",
    "FakeMediaServer",
    "FakeMediaServers",
    "build_app",
    "claim",
    "complete_setup",
    "configure_media_server",
    "console_client",
    "console_headers",
    "cookies_of",
    "media_user",
    "run",
    "server_config",
    "services_of",
    "setup_code",
    "sign_in_console",
]
