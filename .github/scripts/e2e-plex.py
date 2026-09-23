#!/usr/bin/env python3
"""The Plex half of the end-to-end stack — dispatched runs only.

Plex cannot be tested the way Jellyfin and Emby are. A Plex server is only useful to
Tindarr once it is **claimed** by a real plex.tv account (docs/auth.md, section 6: the
connector matches the server's ``machineIdentifier`` against that account's resources
and requires ``owned``), and the sign-in flow goes through plex.tv, not the server. So
this needs a real account, which is why the job is `workflow_dispatch` only and a pull
request from a fork can never reach the secrets.

    e2e-plex.py up          start and claim a throwaway server, start Tindarr,
                            append the Plex target to the environment file
    e2e-plex.py check-logs  fail if the Tindarr server logged an error
    e2e-plex.py down        remove the container and everything the run left on plex.tv

Two secrets are read from the environment:

* ``PLEX_ACCOUNT_TOKEN`` — the account that will own the throwaway server. It is also
  what approves the PINs the console creates (``PUT /api/v2/pins/link``).
* ``PLEX_CLAIM_TOKEN`` — optional. Claim tokens from https://plex.tv/claim last a few
  minutes, so a stored one is almost always stale; when it is absent a fresh one is
  minted from the account token instead.

**Honestly labelled:** unlike `seed-media-server.py`, none of this was run against a
real Plex account — this repository has none. ``PUT /api/v2/pins/link`` is not in
Plex's documentation either; it is what python-plexapi's ``MyPlexAccount.link()`` and
several other clients use. Treat the first dispatched run as the test of this file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("E2E_ENV_FILE", REPO / "e2e.env"))
IMAGE = os.environ.get("E2E_IMAGE", "tindarr-server:e2e")
# renovate: datasource=docker depName=plexinc/pms-docker versioning=loose
PLEX_IMAGE = "plexinc/pms-docker:1.43.4.10903-e5521bd8c@sha256:e0ab27395614a8e1a4fdf84c6bc60ac664915cfdde70c52d030c7728a1c48e14"
PLEX_CONTAINER = "tindarr-e2e-media-plex"
TINDARR_CONTAINER = "tindarr-e2e-server-plex"
PLEX_PORT = 32400
CONSOLE_PORT = 18804
PLEX_TV = "https://plex.tv"
#: Identifies this script to plex.tv. Never the install id Tindarr uses.
CLIENT_ID = "tindarr-e2e-ci"
PRODUCT = "Tindarr e2e"


class PlexError(RuntimeError):
    """plex.tv or the Plex container did not behave as expected."""


def run(*command: str, check: bool = True) -> str:
    result = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if check and result.returncode != 0:
        msg = f"{' '.join(command)} failed: {result.stderr.strip()}"
        raise PlexError(msg)
    return result.stdout


def plex_tv(
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    form: dict[str, str] | None = None,
    accept: str = "application/json",
) -> tuple[int, Any]:
    """Call plex.tv with the headers every Plex client sends."""
    headers = {
        "Accept": accept,
        "X-Plex-Product": PRODUCT,
        "X-Plex-Client-Identifier": CLIENT_ID,
    }
    if token:
        headers["X-Plex-Token"] = token
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(  # noqa: S310 - constant https base
        PLEX_TV + path, data=data, method=method, headers=headers
    )
    opener = urllib.request.build_opener()
    try:
        with opener.open(request, timeout=30) as response:
            raw, status = response.read(), response.status
    except urllib.error.HTTPError as error:
        raw, status = error.read(), error.code
    if not raw:
        return status, None
    if accept == "application/json":
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError:
            return status, None
    return status, ElementTree.fromstring(raw)  # noqa: S314 - plex.tv, over TLS


def claim_token(account_token: str) -> str:
    """Return a claim token, preferring a fresh one over a stored, likely expired one."""
    status, payload = plex_tv("/api/claim/token.json", token=account_token)
    if status == 200 and isinstance(payload, dict) and payload.get("token"):  # noqa: PLR2004
        return str(payload["token"])
    stored = os.environ.get("PLEX_CLAIM_TOKEN", "")
    if stored:
        return stored
    msg = f"could not mint a claim token (status {status}) and PLEX_CLAIM_TOKEN is not set"
    raise PlexError(msg)


def server_identity(timeout: float) -> str:
    """Poll the container's ``/identity`` until it answers claimed, return its id."""
    deadline = time.monotonic() + timeout
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback http, our own container
                f"http://127.0.0.1:{PLEX_PORT}/identity", timeout=5
            ) as response:
                root = ElementTree.fromstring(response.read())  # noqa: S314 - our own container
        except (OSError, ElementTree.ParseError) as error:
            last = str(error)
        else:
            identifier = root.get("machineIdentifier") or ""
            # `claimed` flips locally the moment the token is written: no plex.tv
            # round trip, so it is the one deterministic signal here.
            if root.get("claimed") == "1" and identifier:
                return identifier
            last = f"claimed={root.get('claimed')}"
        time.sleep(2)
    msg = f"the Plex server never came up claimed ({last})"
    raise PlexError(msg)


def wait_for_resource(account_token: str, identifier: str, timeout: float) -> None:
    """Wait until plex.tv lists the server as owned; that is what Tindarr checks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, payload = plex_tv(
            "/api/v2/resources?includeHttps=1&includeRelay=1", token=account_token
        )
        if status == 200 and isinstance(payload, list):  # noqa: PLR2004
            for resource in payload:
                if resource.get("clientIdentifier") == identifier and resource.get("owned"):
                    return
        time.sleep(5)
    msg = f"plex.tv never listed {identifier} as an owned server"
    raise PlexError(msg)


def up() -> None:
    account_token = os.environ.get("PLEX_ACCOUNT_TOKEN", "")
    if not account_token:
        msg = "PLEX_ACCOUNT_TOKEN is not set"
        raise PlexError(msg)

    run(
        "docker", "run", "-d", "--name", PLEX_CONTAINER,
        "-e", f"PLEX_CLAIM={claim_token(account_token)}",
        # First-run only, and it saves a recursive chown of the config volume.
        "-e", "CHANGE_CONFIG_DIR_OWNERSHIP=false",
        "-e", f"ADVERTISE_IP=http://127.0.0.1:{PLEX_PORT}/",
        "-p", f"127.0.0.1:{PLEX_PORT}:32400",
        PLEX_IMAGE,
    )  # fmt: skip
    identifier = server_identity(300)
    wait_for_resource(account_token, identifier, 180)

    run("docker", "volume", "create", TINDARR_CONTAINER)
    run(
        "docker", "run", "-d", "--name", TINDARR_CONTAINER, "--network", "host",
        "--read-only", "--tmpfs", "/tmp:size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--memory", "512m", "--pids-limit", "256",
        "-e", "TINDARR_HOST=127.0.0.1", "-e", f"TINDARR_PORT={CONSOLE_PORT}",
        "-e", "TINDARR_LOG_LEVEL=INFO",
        "-v", f"{TINDARR_CONTAINER}:/data", IMAGE,
    )  # fmt: skip

    code = ""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not code:
        code = run("docker", "exec", TINDARR_CONTAINER, "cat", "/data/setup-code", check=False)
        code = code.strip()
        if not code:
            time.sleep(1)
    if not code:
        msg = "the Tindarr server never wrote its setup code"
        raise PlexError(msg)

    with ENV_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"E2E_PLEX_URL=http://127.0.0.1:{CONSOLE_PORT}\n")
        handle.write("E2E_PLEX_KIND=plex\n")
        handle.write(f"E2E_PLEX_MEDIA_URL=http://127.0.0.1:{PLEX_PORT}\n")
        handle.write(f"E2E_PLEX_SETUP_CODE={code}\n")
    ENV_FILE.chmod(0o600)
    print(f"Plex target ready: console on http://127.0.0.1:{CONSOLE_PORT}")


def check_logs() -> None:
    logs = run("docker", "logs", TINDARR_CONTAINER, check=False)
    bad = []
    for line in logs.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            bad.append(line)
            continue
        if entry.get("level") in ("ERROR", "CRITICAL"):
            bad.append(line)
    if bad:
        print("\n".join(bad[:40]))
        msg = "the Tindarr server logged errors"
        raise PlexError(msg)
    print("no error in the Tindarr server log")


def down() -> None:
    """Remove the containers, then everything this run left on the account.

    A linked PIN becomes an authorised device, and a claimed server becomes a resource.
    Both are deleted by their plex.tv device id; the ones to delete are recognised by
    the product name, which is Tindarr's for the console's PINs and this script's for
    anything it created itself.
    """
    account_token = os.environ.get("PLEX_ACCOUNT_TOKEN", "")
    for container in (PLEX_CONTAINER, TINDARR_CONTAINER):
        run("docker", "rm", "-f", "-v", container, check=False)
    run("docker", "volume", "rm", "-f", TINDARR_CONTAINER, check=False)
    if not account_token:
        return
    status, root = plex_tv("/devices.xml", token=account_token, accept="application/xml")
    if status != 200 or root is None:  # noqa: PLR2004
        print(f"::warning::could not list plex.tv devices (status {status}); clean up by hand")
        return
    for device in root.findall("Device"):
        product = device.get("product") or ""
        name = device.get("name") or ""
        if not (product.startswith("Tindarr") or name.startswith("Tindarr")):
            continue
        device_id = device.get("id")
        if device_id is None:
            continue
        removed, _ = plex_tv(f"/devices/{device_id}.xml", method="DELETE", token=account_token)
        print(f"removed plex.tv device {device_id} ({product} / {name}): {removed}")


def main(argv: list[str]) -> int:
    actions = {"up": up, "check-logs": check_logs, "down": down}
    action = actions.get(argv[1] if len(argv) > 1 else "")
    if action is None:
        print(f"usage: {argv[0]} <up|check-logs|down>", file=sys.stderr)
        return 2
    action()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except PlexError as failure:
        print(f"::error::e2e-plex: {failure}", file=sys.stderr)
        sys.exit(1)
