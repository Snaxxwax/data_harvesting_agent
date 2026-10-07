#!/usr/bin/env python3
"""Desktop notification bridge. API token is read from a private file, never argv."""

import json
import os
import shlex
import subprocess
import urllib.request
from pathlib import Path


def main():
    os.umask(0o077)
    configuration = Path.home() / ".config/harvest/notifications.json"
    config = (
        json.loads(configuration.read_text()) if configuration.exists() else {"ssh_host": "ovh"}
    )
    if config.get("ssh_host"):
        # Reuse Tailscale/SSH; the API token stays inside its existing container.
        code = "import json,os,urllib.request; q=urllib.request.Request('http://127.0.0.1:8000/incidents',headers={'Authorization':'Bearer '+os.environ['HARVEST_API_TOKEN']}); print(json.dumps(json.load(urllib.request.urlopen(q,timeout=10))))"
        current = json.loads(
            subprocess.check_output(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    config["ssh_host"],
                    "docker",
                    "exec",
                    "harvest-platform-api-1",
                    "python",
                    "-c",
                    shlex.quote(code),
                ],
                timeout=20,
            )
        )
    else:
        token = Path(config["token_file"]).read_text().strip()
        request = urllib.request.Request(
            config["api_url"].rstrip("/") + "/incidents",
            headers={"Authorization": "Bearer " + token},
        )
        current = json.load(urllib.request.urlopen(request, timeout=10))
    path = Path.home() / ".local/state/harvest/incidents.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(path.read_text()) if path.exists() else {}
    for row in current:
        key = str(row["id"])
        value = "recovered" if row["resolved"] else "open"
        if previous.get(key) != value:
            subprocess.run(
                ["notify-send", "Harvest: " + row["name"] + " " + value, row["detail"]], check=True
            )
            previous[key] = value
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(previous))
    temporary.replace(path)


if __name__ == "__main__":
    main()
