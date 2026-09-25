#!/usr/bin/env python3
"""Decide what release.yml is about to release, or refuse to release anything.

Reads EVENT, REF_NAME and INPUT_VERSION from the environment and writes `tag`,
`version`, `publish`, `latest` and `created` to $GITHUB_OUTPUT.

The one thing worth reading twice is the comparison between the git tag and the version
in server/pyproject.toml. The server reports its *installed package* version through
/api/v1/server/info, so publishing `1.2.3` from a tree whose project version is
`1.2.3rc1` ships an image that contradicts its own version label — and, because
`1.2.3` is not a pre-release, ships it under `latest`. That is the ordinary shape of
the mistake: a release candidate went out, the tag was bumped, the project version was
not.

So the two are compared **whole**, after normalising each to PEP 440's canonical form,
and never on their stripped release parts: `1.2.3` and `1.2.3rc1` have the same release
part, which is exactly why comparing those is worthless. The single difference in
spelling that is tolerated is the one that cannot be avoided — SemVer writes a release
candidate `1.2.3-rc.1` in a git tag, PEP 440 writes the same version `1.2.3rc1` in
pyproject.toml. Everything PEP 440 can say and this workflow cannot release (epochs,
`.post`, `.dev`, `+local`) is refused by name rather than silently stripped.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import tomllib
from datetime import UTC, datetime

PYPROJECT = pathlib.Path("server/pyproject.toml")

# PEP 440's pre-release spellings, all of which normalise to a, b or rc.
PRE_SPELLINGS = "a|alpha|b|beta|c|pre|preview|rc"
CANONICAL_PRE = {
    "a": "a", "alpha": "a",
    "b": "b", "beta": "b",
    "c": "rc", "pre": "rc", "preview": "rc", "rc": "rc",
}

# What pyproject.toml may say: X.Y.Z, optionally a pre-release of it, in any of the
# spellings PEP 440 allows for it — including the leading zeros PEP 440 tolerates and
# normalises away (`1.02.3` and `1.2.3rc01` are `1.2.3` and `1.2.3rc1`), which is why
# every number here goes through int() before anything is compared.
PROJECT = re.compile(
    rf"(?P<release>\d+\.\d+\.\d+)(?:[-_.]?(?P<pre>{PRE_SPELLINGS})[-_.]?(?P<number>\d+)?)?",
    re.IGNORECASE,
)
# What a git tag may say: the same versions, spelled the SemVer way — and SemVer
# forbids a leading zero, so this one does too. It is not pedantry: node-semver refuses
# `v1.02.3`, docker/metadata-action answers a refusal with `core.warning` and emits no
# version tag at all, and the run stays green with `latest` pointing at an image that
# has no version tag. Refusing the tag here is the first of the two guards against
# that; `release.yml` checking the tag list it actually got is the one that matters.
NUMBER = r"(?:0|[1-9]\d*)"
TAG = re.compile(
    rf"v(?P<release>{NUMBER}\.{NUMBER}\.{NUMBER})"
    rf"(?:-(?P<pre>{PRE_SPELLINGS})[.-]?(?P<number>{NUMBER})?)?"
)

SUPPORTED = "this workflow releases X.Y.Z and pre-releases of it (1.2.3, 1.2.3rc1) and nothing else"


def die(message: str) -> None:
    # The sanitising is done here rather than trusted to every call site: a newline in
    # a message would close the annotation and let the rest start a workflow command of
    # its own. Call sites pass user-supplied values through repr() as well, which is
    # what makes the message readable; this is what makes it safe.
    safe = message.replace("\r", "\\r").replace("\n", "\\n")
    print(f"::error::{safe}")
    raise SystemExit(1)


def why_unsupported(raw: str) -> str:
    """Name the PEP 440 feature that makes a version unreleasable, when there is one."""
    if "!" in raw:
        return "an epoch"
    if "+" in raw:
        return "a local version"
    if re.search(r"(?:^|[-_.])dev", raw, re.IGNORECASE):
        return "a development release"
    if re.search(r"(?:^|[-_.])(?:post|rev|r)\d*$", raw, re.IGNORECASE) or re.fullmatch(
        r"\d+(?:\.\d+)*-\d+", raw
    ):
        return "a post-release"
    if re.search(r"(?:^|[v.])0\d", raw):
        return "a number with a leading zero, which SemVer forbids and metadata-action drops silently"
    return ""


def numbers(match: re.Match[str]) -> tuple[str, str, str]:
    """The release, the pre-release letter and its number, all leading zeros gone.

    PEP 440 normalises `1.02.3rc01` to `1.2.3rc1`, and the installed package therefore
    answers `1.2.3rc1`; comparing the spellings rather than the numbers would refuse a
    legitimate pair. An absent pre-release number is 0, again per PEP 440.
    """
    release = ".".join(str(int(part)) for part in match["release"].split("."))
    if not match["pre"]:
        return release, "", ""
    return release, CANONICAL_PRE[match["pre"].lower()], str(int(match["number"] or 0))


def canonical(match: re.Match[str]) -> str:
    """PEP 440's canonical spelling of a version this workflow accepts."""
    release, pre, number = numbers(match)
    return f"{release}{pre}{number}"


def semver(match: re.Match[str]) -> str:
    """The same version, spelled the way a git tag spells it."""
    release, pre, number = numbers(match)
    return f"v{release}-{pre}.{number}" if pre else f"v{release}"


def main() -> None:
    event = os.environ["EVENT"]
    raw = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    project_match = PROJECT.fullmatch(raw)
    if not project_match:
        detail = why_unsupported(raw)
        detail = f" it is {detail}, and" if detail else ""
        die(
            f"server/pyproject.toml says version = {raw!r}:{detail} {SUPPORTED}. "
            "Set a release version there in the commit you tag."
        )
    project = canonical(project_match)

    if event == "push":
        tag = os.environ["REF_NAME"]
        publish = "true"
    else:
        # Rehearsing anything other than what this tree would release needs the input.
        tag = os.environ["INPUT_VERSION"] or semver(project_match)
        publish = "false"

    # fullmatch over the whole string, so a value carrying a newline is refused here
    # rather than reaching $GITHUB_OUTPUT with a second line of its own.
    tag_match = TAG.fullmatch(tag)
    if not tag_match:
        detail = why_unsupported(tag)
        detail = f" it is {detail}, and" if detail else ""
        die(f"{tag!r} is not a version this workflow can release:{detail} {SUPPORTED}, tagged vX.Y.Z or vX.Y.Z-rc.N.")

    version = canonical(tag_match)
    if version != project:
        die(
            f"the tag {tag!r} is version {version}, but server/pyproject.toml says {raw!r}. "
            f"The image would answer {raw!r} from /api/v1/server/info while being published as {version}. "
            "Bump the project version in the commit you tag."
        )

    # `latest` only for a stable release: a pre-release must never become what a
    # `:latest` deployment pulls next.
    latest = "false" if tag_match["pre"] else "true"

    outputs = {
        "tag": tag,
        # The SemVer spelling, which is what the registry tags and the version label
        # use; `version` above is the same version spelled PEP 440's way.
        "version": tag.removeprefix("v"),
        "publish": publish,
        "latest": latest,
        "created": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if any("\n" in value or "\r" in value for value in outputs.values()):
        die("refusing to write a multi-line value to GITHUB_OUTPUT")

    with pathlib.Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")

    print(f"{tag}: version {version}, publish={publish}, latest={latest}", file=sys.stderr)


if __name__ == "__main__":
    main()
