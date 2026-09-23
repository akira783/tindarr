#!/usr/bin/env python3
"""Seed a throwaway Jellyfin or Emby container for the end-to-end workflow.

Runs the product's own first-run wizard over HTTP, exactly as its web installer does,
then creates what the Tindarr end-to-end scenario needs:

* an administrator account (``POST /Startup/User``),
* an **administrator API key** (``POST /Auth/Keys?app=…``, read back from
  ``GET /Auth/Keys``): what the console's media server step asks for,
* a **second, non-administrator account**, so the scenario can check that a plain user
  lands on "Connect a phone" instead of the administration pages.

Everything here was checked against real containers (Jellyfin 10.10.7, Jellyfin 12.1,
Emby 4.10) before being written; the differences between the two products are marked.
Only the standard library is used, so it runs on a bare GitHub runner.

Usage::

    seed-media-server.py --kind jellyfin --url http://127.0.0.1:18096 \
        --admin tindarr-admin --admin-password … --user tindarr-user --user-password …

It prints ``NAME=value`` lines on stdout for the caller to source:
``MEDIA_API_KEY``, ``MEDIA_SERVER_ID``, ``MEDIA_SERVER_NAME``, ``MEDIA_SERVER_VERSION``.
Re-running it against an already seeded server is a no-op.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: The four values Jellyfin and Emby identify a client by. The seeding client is *not*
#: Tindarr: using another device id keeps Tindarr's own sessions out of the way, since
#: both products revoke a user's earlier token for the same device id at each sign-in.
CLIENT_HEADER = (
    'MediaBrowser Client="Tindarr e2e seed", Device="ci", DeviceId="tindarr-e2e-seed", Version="1"'
)
#: ``GET /Startup/Configuration`` answers 200 while the wizard is pending and 401 once
#: it is finished, on both products. Jellyfin also has ``StartupWizardCompleted`` in
#: ``/System/Info/Public``; Emby has no such field, so this is the shared probe.
WIZARD_PROBE = "/Startup/Configuration"
HTTP_OK = 200
HTTP_UNAUTHORIZED = 401


class SeedError(RuntimeError):
    """The media server did not answer the way its API is documented to."""


def _request(
    url: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    """Call the media server and return ``(status, parsed body or None)``.

    A 4xx or 5xx answer is returned like any other: the callers decide what an error
    status means (``401`` on the wizard probe is information, not a failure).
    """
    header = CLIENT_HEADER + (f', Token="{token}"' if token else "")
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - http(s) only, built from our own arguments
        url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Authorization": header, "Accept": "application/json"}
        | ({"Content-Type": "application/json"} if data is not None else {}),
    )
    if urllib.parse.urlparse(request.full_url).scheme not in ("http", "https"):
        msg = "only http(s) URLs are supported"
        raise SeedError(msg)
    # No proxy: the target is a container on this host.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, None


def _expect(
    url: str, path: str, *, method: str = "GET", body: Any = None, token: str | None = None
) -> Any:
    """Call the server and fail unless it answered 2xx."""
    status, payload = _request(url, path, method=method, body=body, token=token)
    if not (HTTP_OK <= status < 300):  # noqa: PLR2004 - "2xx"
        msg = f"{method} {path} answered {status}"
        raise SeedError(msg)
    return payload


def _field(payload: Any, name: str) -> Any:
    """Read a field whatever its casing.

    Jellyfin answers in PascalCase once it is running, but a request that reaches it in
    the first seconds of startup can come back camelCase; Emby is PascalCase throughout.
    Reading case-insensitively costs nothing and removes a startup race.
    """
    if not isinstance(payload, dict):
        return None
    lowered = {str(key).lower(): value for key, value in payload.items()}
    return lowered.get(name.lower())


def wait_ready(url: str, timeout: float) -> dict[str, Any]:
    """Poll until the server is really up, and return ``GET /System/Info/Public``.

    "Really up" means two things, because Jellyfin answers before it is finished
    starting: ``/System/Info/Public`` carries a server id, **and** the wizard probe
    gives one of its two meaningful answers. While Jellyfin 12 is still booting the
    probe answers ``503`` and the public info comes back in camelCase from a different
    pipeline — both would make the seeding take the wrong branch.
    """
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            status, payload = _request(url, "/System/Info/Public", timeout=5.0)
            probe, _ = _request(url, WIZARD_PROBE, timeout=5.0)
        except OSError as error:
            last = str(error)
        else:
            if status == HTTP_OK and _field(payload, "Id") and probe in (HTTP_OK, HTTP_UNAUTHORIZED):
                return payload if isinstance(payload, dict) else {}
            last = f"/System/Info/Public {status}, {WIZARD_PROBE} {probe}"
        time.sleep(1.0)
    msg = f"{url} was not ready after {timeout:.0f} s ({last})"
    raise SeedError(msg)


def wizard_pending(url: str) -> bool:
    """True while the first-run wizard still has to be answered.

    ``GET /Startup/Configuration`` is open while the wizard is pending (Jellyfin's
    ``FirstTimeSetupOrElevated`` policy lets anyone through until it completes, and
    Emby behaves the same) and answers ``401`` once it is done. Only ``wait_ready``
    above may see anything else.
    """
    status, _ = _request(url, WIZARD_PROBE)
    if status == HTTP_OK:
        return True
    if status == HTTP_UNAUTHORIZED:
        return False
    msg = f"GET {WIZARD_PROBE} answered {status}: cannot tell whether the wizard is done"
    raise SeedError(msg)


def run_wizard(url: str, kind: str, admin: str, password: str) -> None:
    """Answer the first-run wizard: locale, administrator, remote access, done."""
    _expect(
        url,
        "/Startup/Configuration",
        method="POST",
        body={
            "UICulture": "en-US",
            "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en",
            # Jellyfin 12 added this field and writes it on every POST, so leaving it
            # out would blank the server's own name. Older builds ignore it.
            "ServerName": f"Tindarr e2e {kind}",
        },
    )
    # **Not optional, and not a read.** ``GET /Startup/User`` is the only call that
    # initialises the user manager: without it the default user does not exist yet and
    # the POST below answers 500 (Jellyfin 10.10, "Sequence contains no elements") or
    # 404 (10.11 and 12). The web wizard does the same GET when it shows the page.
    _expect(url, "/Startup/User")
    _expect(url, "/Startup/User", method="POST", body={"Name": admin, "Password": password})
    # Both products accept it; Jellyfin's wizard always posts it, and it is what sets
    # the administrator's EnableRemoteAccess, which Tindarr reads at every sign-in.
    _expect(
        url,
        "/Startup/RemoteAccess",
        method="POST",
        body={"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False},
    )
    _expect(url, "/Startup/Complete", method="POST")


def sign_in(url: str, name: str, password: str) -> tuple[str, str]:
    """Sign in with ``POST /Users/AuthenticateByName`` and return ``(token, user id)``."""
    payload = _expect(
        url, "/Users/AuthenticateByName", method="POST", body={"Username": name, "Pw": password}
    )
    token = _field(payload, "AccessToken")
    user_id = _field(_field(payload, "User"), "Id")
    if not isinstance(token, str) or not isinstance(user_id, str):
        msg = "the sign-in answer carried no access token"
        raise SeedError(msg)
    return token, user_id


def ensure_api_key(url: str, token: str, app_name: str) -> str:
    """Return an administrator API key named ``app_name``, creating it when needed.

    ``POST /Auth/Keys?app=<name>`` answers 204 and never returns the key: it has to be
    read back from ``GET /Auth/Keys``, whose rows carry ``AppName`` and ``AccessToken``.
    Both products behave this way.
    """
    for attempt in (0, 1):
        listing = _expect(url, "/Auth/Keys", token=token)
        for item in _field(listing, "Items") or []:
            if _field(item, "AppName") == app_name:
                key = _field(item, "AccessToken")
                if isinstance(key, str) and key:
                    return key
        if attempt == 0:
            _expect(
                url,
                "/Auth/Keys?app=" + urllib.parse.quote(app_name),
                method="POST",
                token=token,
            )
    msg = f"the API key {app_name!r} was created but does not appear in GET /Auth/Keys"
    raise SeedError(msg)


def ensure_user(url: str, token: str, kind: str, name: str, password: str) -> str:
    """Create a second, non-administrator account and give it a password.

    ``POST /Users/New`` creates a plain user on both products (``IsAdministrator`` is
    false by default, which is exactly what the scenario needs). Jellyfin takes the
    password in that same call; Emby ignores it and needs
    ``POST /Users/{id}/Password`` with ``CurrentPw``/``NewPw``.
    """
    existing = {
        _field(user, "Name"): _field(user, "Id") for user in _expect(url, "/Users", token=token)
    }
    user_id = existing.get(name)
    if user_id is None:
        body = {"Name": name} | ({"Password": password} if kind == "jellyfin" else {})
        created = _expect(url, "/Users/New", method="POST", body=body, token=token)
        user_id = _field(created, "Id")
        if not isinstance(user_id, str):
            msg = "POST /Users/New returned no user id"
            raise SeedError(msg)
        if kind == "emby":
            _expect(
                url,
                f"/Users/{user_id}/Password",
                method="POST",
                body={"CurrentPw": "", "NewPw": password},
                token=token,
            )
    return user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", required=True, choices=["jellyfin", "emby"])
    parser.add_argument("--url", required=True, help="Base URL of the media server.")
    parser.add_argument("--admin", required=True)
    parser.add_argument("--admin-password", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--user-password", required=True)
    parser.add_argument("--app-name", default="Tindarr e2e")
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args(argv)

    info = wait_ready(args.url, args.timeout)
    if wizard_pending(args.url):
        run_wizard(args.url, args.kind, args.admin, args.admin_password)
        info = wait_ready(args.url, 60.0)

    token, _ = sign_in(args.url, args.admin, args.admin_password)
    api_key = ensure_api_key(args.url, token, args.app_name)
    ensure_user(args.url, token, args.kind, args.user, args.user_password)

    print(f"MEDIA_API_KEY={api_key}")
    print(f"MEDIA_SERVER_ID={_field(info, 'Id')}")
    print(f"MEDIA_SERVER_NAME={_field(info, 'ServerName')}")
    print(f"MEDIA_SERVER_VERSION={_field(info, 'Version')}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SeedError as failure:
        print(f"::error::seed-media-server: {failure}", file=sys.stderr)
        sys.exit(1)
