#!/usr/bin/env python3
"""Render root-only gateway config from the running API; never print credentials."""

import json
import os
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
site = dict(
    line.split("=", 1)
    for line in (root / "site.env").read_text().splitlines()
    if line.strip() and not line.startswith("#")
)
container = json.loads(subprocess.check_output(["docker", "inspect", "harvest-platform-api-1"]))[0]
env = dict(value.split("=", 1) for value in container["Config"]["Env"])
config = (root / "nginx.conf.template").read_text()
for key, pattern in [("TAILNET_HOST", r"[a-z0-9.-]+\.ts\.net"), ("UPSTREAM_IP", r"[0-9.]+")]:
    if not re.fullmatch(pattern, site[key]):
        raise SystemExit(f"site.env {key} is not a valid value")
    config = config.replace("@@" + key + "@@", site[key])
for placeholder, variable in [
    ("HARVEST_TOKEN", "HARVEST_API_TOKEN"),
    ("SPIDERFOOT_KEY", "HARVEST_SPIDERFOOT_API_KEY"),
]:
    value = env[variable]
    if not re.fullmatch(r"[A-Za-z0-9_.=+/\-]{24,}", value):
        raise SystemExit(f"{variable} cannot be safely rendered as an nginx literal")
    config = config.replace("@@" + placeholder + "@@", value)
fd = os.open(root / "nginx.conf", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as output:
    output.write(config)
os.chmod(root / "nginx.conf", 0o600)
image = json.loads(subprocess.check_output(["docker", "inspect", "sf-frontend-ui"]))[0]["Image"]
(root / ".env").write_text("GATEWAY_IMAGE=" + image + "\n")
print("Rendered gateway configuration; credentials remain on the server.")
