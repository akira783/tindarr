"""Test doubles and helpers shared by the step-2 test modules.

``fakes`` holds a controllable clock and a fake media server implementing the port;
``console`` builds HTTP clients that behave like the web console (https origin, cookies,
CSRF header). Steps 2b and 2c build their own fakes on these.
"""

from tests.support.fakes import FakeClock, FakeMediaServer, FakeMediaServers, media_user

__all__ = ["FakeClock", "FakeMediaServer", "FakeMediaServers", "media_user"]
