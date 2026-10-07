from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path


def notify(title: str, body: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "--app-name=Harvest", title, body],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def main() -> None:
    base = os.environ["HARVEST_API_URL"].rstrip("/")
    token = os.environ["HARVEST_API_TOKEN"]
    state_path = Path(
        os.environ.get(
            "HARVEST_INCIDENT_STATE",
            "~/.cache/harvest/incident-notifier.json",
        )
    ).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        previous = {}

    request = urllib.request.Request(
        base + "/incidents?limit=200",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            rows = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        # A sleeping/offline workstation cannot guarantee notification delivery; the
        # server-side incident remains durable and will be picked up next time.
        return

    latest: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("incident_key") or "")
        if key and key not in latest:
            latest[key] = row
    active = {key: row for key, row in latest.items() if row.get("resolved") is None}
    before = set(previous.get("active", []))
    now = set(active)

    for key in sorted(now - before):
        row = active[key]
        detail = row.get("details") or {}
        notify(
            f"Harvest: {row.get('severity', 'warning')}",
            f"{key}\n{json.dumps(detail, sort_keys=True)[:500]}",
        )
    for key in sorted(before - now):
        notify("Harvest recovered", key)

    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"active": sorted(now)}, indent=2))
    temporary.replace(state_path)


if __name__ == "__main__":
    main()
