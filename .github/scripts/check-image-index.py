#!/usr/bin/env python3
"""Check that an OCI image index holds the architectures it should, each attested.

Two ways in, one set of assertions, because the release rehearsal and the real
publication must be checked by the same code:

    check-image-index.py --oci-layout DIR            # what `type=oci,dest=` wrote
    docker buildx imagetools inspect --raw REF | check-image-index.py -

`--expect` is a comma-separated list of os/arch, defaulting to what release.yml
publishes. An index missing an architecture, or holding one it was never asked for, or
carrying a platform manifest with no BuildKit attestation manifest pointing at it, is a
failure: the SBOM and the provenance are per-architecture, and an architecture without
them is one nobody can check.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ATTESTATION = "attestation-manifest"


def load_from_layout(directory: pathlib.Path) -> dict:
    """Resolve the single image index an OCI layout directory points at."""
    index = json.loads((directory / "index.json").read_text())
    manifests = index.get("manifests", [])
    if len(manifests) != 1:
        raise SystemExit(f"expected one manifest in index.json, found {len(manifests)}")
    top = manifests[0]
    if "index" not in top["mediaType"]:
        raise SystemExit(f"expected an image index, got {top['mediaType']}")
    algorithm, digest = top["digest"].split(":")
    return json.loads((directory / "blobs" / algorithm / digest).read_bytes())


def check(index: dict, expected: set[str]) -> None:
    if "index" not in index.get("mediaType", ""):
        raise SystemExit(f"expected an image index, got {index.get('mediaType')!r}")

    platforms: dict[str, str] = {}
    attested: set[str] = set()
    for manifest in index.get("manifests", []):
        annotations = manifest.get("annotations") or {}
        if annotations.get("vnd.docker.reference.type") == ATTESTATION:
            attested.add(annotations["vnd.docker.reference.digest"])
            continue
        platform = manifest.get("platform") or {}
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        platforms[manifest["digest"]] = name

    found = set(platforms.values())
    if found != expected:
        raise SystemExit(f"expected {sorted(expected)}, got {sorted(found)}")

    unattested = sorted(name for digest, name in platforms.items() if digest not in attested)
    if unattested:
        raise SystemExit(f"no attestation manifest for: {unattested}")

    print(f"index holds {sorted(found)}, each with its attestations")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--oci-layout", type=pathlib.Path, help="an OCI layout directory")
    source.add_argument("index", nargs="?", help="an index JSON file, or - for stdin")
    parser.add_argument("--expect", default="linux/amd64,linux/arm64")
    args = parser.parse_args()

    expected = {item.strip() for item in args.expect.split(",") if item.strip()}
    if args.oci_layout:
        index = load_from_layout(args.oci_layout)
    elif args.index == "-":
        index = json.load(sys.stdin)
    else:
        index = json.loads(pathlib.Path(args.index).read_text())
    check(index, expected)


if __name__ == "__main__":
    main()
