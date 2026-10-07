#!/usr/bin/env python3
"""Resolve tested local builds and unchanged sidecars into immutable Compose image IDs."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def inspect(kind, reference):
    return json.loads(subprocess.check_output(["docker", kind, "inspect", reference]))[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--harvest", required=True)
    parser.add_argument("--spiderfoot-api", required=True)
    parser.add_argument("--spiderfoot-scanner", required=True)
    parser.add_argument(
        "--spiderfoot-frontend", default="sf-frontend", help="unchanged frontend container"
    )
    parser.add_argument(
        "--flaresolverr",
        default="harvest-platform-flaresolverr-1",
        help="unchanged solver container",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pins = {
        "HARVEST_IMAGE": inspect("image", args.harvest)["Id"],
        "SF_API_IMAGE": inspect("image", args.spiderfoot_api)["Id"],
        "SF_SCANNER_IMAGE": inspect("image", args.spiderfoot_scanner)["Id"],
        "SF_FRONTEND_IMAGE": inspect("container", args.spiderfoot_frontend)["Image"],
        "FLARESOLVERR_IMAGE": inspect("container", args.flaresolverr)["Image"],
    }
    if not all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in pins.values()):
        raise ValueError("immutable image IDs required")
    # Refuse accidental replacement of the previous release's rollback identities.
    with args.output.open("x") as stream:
        stream.write("".join(f"{name}={value}\n" for name, value in pins.items()))
    print(json.dumps(pins, indent=2))


if __name__ == "__main__":
    main()
