"""Readiness sampling and durable incident transitions; health probes never acquire targets."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import httpx

from .store import packed


def state(store, name, value):
    with store.transaction() as db:
        db.execute(
            "INSERT INTO operational_state VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated=excluded.updated",
            (name, packed(value), time.time()),
        )


def read_state(store, name):
    with store.connection() as db:
        row = db.execute(
            "SELECT value,updated FROM operational_state WHERE name=?", (name,)
        ).fetchone()
    return (json.loads(row[0]), row[1]) if row else (None, 0)


def incident(store, name, healthy, detail):
    with store.transaction() as db:
        current = db.execute(
            "SELECT id FROM incidents WHERE name=? AND resolved IS NULL", (name,)
        ).fetchone()
        if healthy and current:
            db.execute("UPDATE incidents SET resolved=? WHERE id=?", (time.time(), current[0]))
        elif not healthy and not current:
            db.execute(
                "INSERT INTO incidents(name,opened,detail) VALUES(?,?,?)",
                (name, time.time(), detail),
            )


def inspect(store, settings):
    checks = {}

    def check(name, ok, detail):
        checks[name] = {"ready": bool(ok), "detail": detail}

    with store.connection() as db:
        db.execute("SELECT 1")
    check("database", True, "SQLite readable")
    _, beat = read_state(store, "worker_heartbeat")
    check("worker", time.time() - beat < 60, "worker heartbeat must be newer than 60 seconds")
    disk = shutil.disk_usage(Path(store.path).parent)
    free = disk.free / disk.total
    check("disk", free >= 0.20, f"{free:.1%} free; admission stops below 10%")
    receipt = Path(store.path).parent / "backup-verified.json"
    try:
        backup = json.loads(receipt.read_text())
        verified = float(backup["verified_at"])
        created = float(backup["created"])
    except (OSError, ValueError, KeyError, TypeError):
        backup, verified, created = None, 0, 0
    check(
        "backup",
        bool(backup)
        and 0 <= time.time() - verified <= 86400
        and 0 <= time.time() - created <= 86400,
        "verified off-host copy and its source backup must both be newer than 24 hours",
    )
    for name in sorted(settings.tools & {"maigret", "ghunt"}):
        check(
            name,
            shutil.which(name),
            "binary installed" if shutil.which(name) else "binary unavailable",
        )
    if "ghunt" in settings.tools:
        check(
            "ghunt_credentials",
            (Path(os.environ.get("HOME", "")) / ".malfrats/ghunt/creds.m").is_file(),
            "persistent GHunt credentials present (validity established by acquisition)",
        )
    if "spiderfoot" in settings.tools:
        ok = False
        if settings.spiderfoot_url and settings.spiderfoot_api_key:
            try:
                with httpx.Client(timeout=5, trust_env=False) as client:
                    r = client.get(
                        settings.spiderfoot_url + "/api/v1/scans",
                        params={"limit": 1},
                        headers={"X-API-Key": settings.spiderfoot_api_key},
                    )
                    ok = r.status_code == 200 and isinstance(r.json(), dict)
            except (httpx.HTTPError, ValueError):
                pass
        check("spiderfoot", ok, "authenticated SpiderFoot connectivity")
    proxy_ok = not settings.proxy_only
    if settings.proxy_only:
        from .models import PolicyDenied
        from .tools import _assert_no_direct_egress

        try:
            _assert_no_direct_egress(settings)
            with httpx.Client(proxy=settings.proxy, timeout=8, trust_env=False) as client:
                proxy_ok = client.get("https://example.com/").status_code == 200
        except (httpx.HTTPError, OSError, PolicyDenied):
            proxy_ok = False
    check("proxy", proxy_ok, "required proxy connectivity and direct egress confinement")
    if settings.search_url:
        search, at = read_state(store, "search_canary")
        check(
            "search",
            search and search.get("ready") and time.time() - at < 2100,
            "known-answer search must find a relevant source; last sample expires after 35 minutes",
        )
    return {
        "ready": all(x["ready"] for x in checks.values()),
        "checked_at": time.time(),
        "checks": checks,
    }


def search_canary(store, settings):
    """One query per 30 minutes through the deployment search service and its proxy route."""
    try:
        with httpx.Client(timeout=25, trust_env=False) as client:
            response = client.get(
                settings.search_url.rstrip("/") + "/search",
                params={"q": "site:iana.org reserved example domains", "format": "json"},
            )
            response.raise_for_status()
            payload = response.json()
        from .search import known_answer

        result = known_answer(payload)
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        result = {"ready": False, "reason": "canary request or parser failed"}
    state(store, "search_canary", result)
    for provider in result.get("providers", []):
        incident(
            store,
            "search_provider_" + provider["engine"],
            provider["outcome"] == "completed",
            provider["reason"] or "known-answer search recovered",
        )
    incident(
        store,
        "search_providers",
        result["ready"] and all(p["outcome"] == "completed" for p in result.get("providers", [])),
        "search canary failed or a provider is degraded",
    )
    return result


def monitor(store, settings, stop):
    next_search = 0
    while not stop.is_set():
        if settings.search_url and time.monotonic() >= next_search:
            search_canary(store, settings)
            next_search = time.monotonic() + 1800
        try:
            result = inspect(store, settings)
            state(store, "readiness", result)
            incident(store, "monitor", True, "readiness sampling recovered")
            for name, check in result["checks"].items():
                incident(store, name, check["ready"], check["detail"])
        except Exception:
            # No raw exceptions: transport messages may echo credentials.
            incident(store, "monitor", False, "readiness sampling failed")
        stop.wait(60)


def main():
    import signal
    import threading

    from .config import Settings
    from .store import Store

    settings = Settings()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    monitor(Store(settings.database), settings, stop)


if __name__ == "__main__":
    main()
