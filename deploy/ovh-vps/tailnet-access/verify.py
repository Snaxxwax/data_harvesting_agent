#!/usr/bin/env python3
"""Read-only regression checks; run from a connected tailnet device."""

import urllib.error
import urllib.request
from pathlib import Path

site = dict(
    line.split("=", 1)
    for line in (Path(__file__).resolve().parent / "site.env").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)


def status(url, expected, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            actual = response.status
    except urllib.error.HTTPError as error:
        actual = error.code
    assert actual == expected, (url, actual, expected)
    print(f"PASS {expected} {url}")


host = "https://" + site["TAILNET_HOST"]
upstream = "http://" + site["UPSTREAM_IP"]
# SpiderFoot only: the Harvest route (:443 -> 18083) was retired with Harvest on 2026-11-10.
status(host + ":8443/api/auth/me", 200)
status(host + ":8443/api/auth/me", 403, {"Origin": "https://untrusted.example"})
status(upstream + ":8001/api/auth/me", 401)
status(
    upstream + ":8001/api/auth/me",
    401,
    {"Authorization": "Bearer tailnet-proxy", "Tailscale-User-Login": "spoofed"},
)
