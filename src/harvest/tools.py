"""External OSINT CLIs (Maigret, GHunt, ...) as durable acquisition tasks.

A tool run is an acquisition, exactly like a fetch or a search: it produces one immutable
capture whose body is the tool's own JSON output, and the existing JSON adapter interprets
it on a separate `extract` task. Nothing here parses into claims -- that stays in extract.py,
so a tool's evidence is replayable offline like any other capture.

Each tool needs its own function because their output contracts genuinely differ (report
file vs stdout, per-site dict vs list). What they share is the subprocess discipline in
`_exec` and the argv-only trust boundary in `run`.

Tools are opt-in per deployment: `Settings.tools` is an allowlist (HARVEST_TOOLS), empty by
default, because these binaries make their own unbudgeted network requests outside Fetcher.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .models import TOOL_TARGET_PATTERN, LostLease, PolicyDenied
from .network import Capture

log = logging.getLogger("harvest")

# ToolRun applies this at submit; re-checked here because tools.run is also called
# directly. fullmatch, not match: "$" would otherwise accept a trailing newline, letting
# "alice\n" through into an argv element, a report filename and a capture URL.
_TARGET_RE = re.compile(TOOL_TARGET_PATTERN)


def _tool_env(proxy: str | None) -> dict[str, str]:
    """Remove ambient proxies; Maigret receives the configured route via --proxy."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    }
    if not proxy:
        return env
    try:
        parsed = urlsplit(proxy)
        _ = parsed.port  # Reject malformed explicit ports; HTTP proxies may use the default.
        valid = parsed.scheme.lower() in {"http", "https", "socks5", "socks5h"} and bool(
            parsed.hostname
        )
    except ValueError:
        valid = False
    if not valid:
        raise PolicyDenied("HARVEST_EGRESS_PROXY is not a supported proxy URL")
    return env


def _stop_process(proc: subprocess.Popen) -> None:
    """Stop the entire tool process group, including any children it started."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()


def _exec(
    argv: list[str],
    timeout: float,
    cwd: str,
    cancelled: threading.Event | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run one tool to completion, or stop it.

    `cancelled` is set by the worker's heartbeat when the task loses its lease, so a
    cancelled job terminates the tool's whole process group instead of leaving a scan
    running to completion. There is deliberately no second, simpler execution path: one
    that skipped Popen would be the path production never takes, and the timeout and
    exit-code rules below are exactly what needs test coverage.
    """
    cancelled = cancelled if cancelled is not None else threading.Event()
    deadline = time.monotonic() + timeout
    try:
        with subprocess.Popen(  # noqa: S603 - argv list, never a shell string
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        ) as proc:
            while True:
                if cancelled.is_set():
                    _stop_process(proc)
                    raise LostLease("tool task cancelled or lease lost")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_process(proc)
                    # Deliberately not RetryLater. Each attempt is a full scan costing
                    # hundreds to thousands of third-party requests outside every budget,
                    # and a run that exceeded its wall clock is not more likely to fit on a
                    # second try. Retrying would make limits.tool_runs bound declarations
                    # rather than actual invocations.
                    raise ValueError(f"{argv[0]} exceeded {timeout:g}s")
                try:
                    # Documented as safe to retry after a timeout without losing output.
                    stdout, _stderr = proc.communicate(timeout=min(1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if proc.returncode != 0:
                    # stderr can contain proxy credentials or tool cookies. Never log it.
                    log.warning("tool_failed argv0=%s rc=%s", argv[0], proc.returncode)
                    # A tool that aborted may still have left a partial report behind;
                    # ingesting it would present an incomplete scan as a finished one. Every
                    # nonzero exit in maigret is a startup/config failure or an interrupt,
                    # never a per-site error.
                    raise ValueError(f"{argv[0]} exited {proc.returncode}")
                return subprocess.CompletedProcess(argv, proc.returncode, stdout, _stderr)
    except FileNotFoundError as exc:
        raise PolicyDenied(f"{argv[0]} is not installed in this worker image") from exc


def _assert_no_direct_egress(settings) -> None:
    """Refuse to run if direct egress works while proxy-only mode is on.

    Maigret's activation helpers issue their own ClientSession calls that never
    receive ``--proxy`` (upstream), so the application cannot itself promise
    proxy-only egress for every request a tool makes.  The guarantee has to come
    from the host's egress policy.

    Rather than trust an operator flag claiming a firewall exists, test it: open
    a plain TCP connection to a public address with no proxy involved.  If it
    connects, direct egress is available and proxy-only mode is a fiction, so the
    run is refused instead of silently leaking.  A blocked probe is the pass.

    This proves the worker's own egress only.  SpiderFoot and FlareSolverr run in
    their own containers with their own policy; see _assert_spiderfoot_proxied.
    """
    host, _, port = settings.egress_probe.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5):
            pass
    except OSError:
        return  # unreachable: the host blocks direct egress, which is the point
    raise PolicyDenied(
        "HARVEST_EGRESS_MODE=proxy but direct egress to "
        f"{settings.egress_probe} succeeded; block outbound traffic except to the "
        "proxy before running tools in proxy-only mode"
    )


def _assert_spiderfoot_proxied(client, settings) -> None:
    """Confirm SpiderFoot's own egress is routed through the same proxy.

    SpiderFoot scans from its own container, so neither ``--proxy`` nor this
    host's egress policy covers it.  It has a global SOCKS/HTTP proxy setting
    (the ``_socks*`` config keys); read it back and refuse the scan unless it
    matches HARVEST_EGRESS_PROXY.  Without this check a proxy-only deployment
    would still send every SpiderFoot module request out directly.
    """
    parsed = urlsplit(settings.proxy)
    want_type = {"socks5": "5", "socks5h": "5", "http": "HTTP", "https": "HTTP"}.get(
        parsed.scheme.lower()
    )
    if not want_type:
        raise PolicyDenied("HARVEST_EGRESS_PROXY scheme is not supported by SpiderFoot")

    got = client.get("/api/v1/config")
    if got.status_code in (401, 403):
        raise PolicyDenied("SpiderFoot rejected HARVEST_SPIDERFOOT_API_KEY")
    got.raise_for_status()
    config = got.json()
    if isinstance(config, dict) and isinstance(config.get("config"), dict):
        config = config["config"]

    actual = (
        str(config.get("_socks1type") or "").upper(),
        str(config.get("_socks2addr") or ""),
        str(config.get("_socks3port") or ""),
    )
    expected = (
        want_type.upper(),
        parsed.hostname or "",
        str(parsed.port or (1080 if want_type == "5" else 8080)),
    )
    if actual != expected:
        raise PolicyDenied(
            "HARVEST_EGRESS_MODE=proxy but SpiderFoot's global proxy does not match "
            "HARVEST_EGRESS_PROXY; set its _socks1type/_socks2addr/_socks3port "
            "(PATCH /api/v1/config) so its modules do not egress directly"
        )


def _maigret(
    target: str,
    workdir: str,
    timeout: float,
    cancelled: threading.Event | None = None,
    settings=None,
) -> list[dict]:
    """`--json simple` writes report_<username>_simple.json: an object keyed by sitename,
    holding only CLAIMED accounts. Flattened to a list so the JSON adapter yields one entity
    per account rather than one entity with 500 nested fields, and `url_user` is surfaced as
    `url` so each found profile becomes both a stable entity key and a crawlable lead.
    """
    argv = [
        "maigret",
        target,
        # All known sites rather than the top-ranked default: roughly ten times the sites,
        # so roughly ten times the outbound requests, none of which pass through the
        # fetcher's budgets. Measured at 309 MiB peak against the worker's 512 MB limit.
        # Some site definitions in the full set raise internally without changing the exit
        # code, so a clean exit with a report remains the success signal.
        "--all-sites",
        "--json",
        "simple",
        "--folderoutput",
        workdir,
        "--no-color",
        "--no-progressbar",
        "--timeout",
        str(max(1, int(min(timeout, 30)))),
    ]
    proxy = getattr(settings, "proxy", None)
    retries = getattr(settings, "maigret_retries", 0)
    cloudflare_bypass = getattr(settings, "maigret_cloudflare_bypass", False)
    if not 0 <= retries <= 3:
        raise PolicyDenied("HARVEST_MAIGRET_RETRIES must be between 0 and 3")
    argv.extend(["--retries", str(retries)])
    proxy_only = bool(getattr(settings, "proxy_only", False))
    if cloudflare_bypass:
        if proxy_only:
            # The bypass service (FlareSolverr) fetches from its own container and
            # is not covered by --proxy or by this host's egress policy, so in
            # proxy-only mode it is an unproxied path by construction.
            raise PolicyDenied(
                "HARVEST_MAIGRET_CLOUDFLARE_BYPASS cannot be used with "
                "HARVEST_EGRESS_MODE=proxy: the bypass service egresses directly"
            )
        # This flag requires a separately configured local bypass service in
        # Maigret's settings. It does not affect routing of ordinary site checks.
        argv.append("--cloudflare-bypass")
    env = _tool_env(proxy)
    # Always use the bundled site database. The pinned Maigret's auto-update makes a
    # direct request to GitHub before the site checks, and it ignores --proxy, so in
    # proxy mode it would leak the one request the proxy exists to cover. Without a
    # proxy it is still an unbudgeted request outside the fetcher, and it would let
    # the site list -- which is authorization-relevant, not just a volume question --
    # change under a deployment without the image changing. Pinning the database to
    # the image keeps a scan's breadth reproducible.
    argv.append("--no-autoupdate")
    if proxy:
        argv.extend(["--proxy", proxy])
        # Maigret's activation helpers use separate ClientSession calls that do
        # not receive --proxy, so --proxy alone is not a no-direct-traffic
        # guarantee until upstream fixes those paths. In proxy-only mode that gap
        # is closed by requiring the host to block direct egress, and verified
        # rather than assumed; otherwise it stays a warning and the operator
        # keeps the historical behaviour.
        if proxy_only:
            _assert_no_direct_egress(settings)
        else:
            log.warning("maigret auxiliary activation requests may bypass --proxy")
    elif proxy_only:  # pragma: no cover - Settings.__post_init__ rejects this
        raise PolicyDenied("HARVEST_EGRESS_MODE=proxy requires HARVEST_EGRESS_PROXY")
    _exec(argv, timeout, workdir, cancelled, env=env)
    # maigret replaces "/" in the username when naming the report; _TARGET_RE already
    # rejects "/", so the name is the target verbatim.
    report = Path(workdir) / f"report_{target}_simple.json"
    if not report.exists():
        raise ValueError("maigret exited cleanly but wrote no JSON report")
    data = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("maigret JSON report was not an object keyed by sitename")
    return [
        {"sitename": name, **site, "url": site.get("url_user")}
        for name, site in data.items()
        if isinstance(site, dict)
    ]


# SpiderFoot NG speaks HTTP rather than argv, so it does not go through _exec. The
# service is infrastructure on a private network, not a scan target, so these calls
# deliberately bypass the fetcher's budgets and robots handling -- the scan SpiderFoot
# then runs is what costs outbound requests, and SpiderFoot bounds that itself.
def _spiderfoot(
    target: str,
    workdir: str,
    timeout: float,
    cancelled: threading.Event | None = None,
    settings=None,
) -> list[dict]:
    """Run one SpiderFoot scan to completion and return its events as records."""
    import httpx

    base = getattr(settings, "spiderfoot_url", "") or ""
    key = getattr(settings, "spiderfoot_api_key", "") or ""
    modules = list(getattr(settings, "spiderfoot_modules", ()) or ())
    if not base:
        raise PolicyDenied("HARVEST_SPIDERFOOT_URL is not configured")
    if not key:
        raise PolicyDenied("HARVEST_SPIDERFOOT_API_KEY is not configured")
    if not modules:
        raise PolicyDenied("HARVEST_SPIDERFOOT_MODULES is empty; no scan would run")

    # The key authenticates every call; it must never reach a log or a persisted error.
    headers = {"X-API-Key": key, "content-type": "application/json"}
    deadline = time.time() + timeout
    scan_id = None
    with httpx.Client(base_url=base, headers=headers, timeout=30.0) as client:
        try:
            if getattr(settings, "proxy_only", False):
                _assert_spiderfoot_proxied(client, settings)
            created = client.post(
                "/api/v1/scans",
                json={
                    "name": f"harvest-{target}",
                    "target": target,
                    "modules": modules,
                },
            )
            if created.status_code in (401, 403):
                raise PolicyDenied("SpiderFoot rejected HARVEST_SPIDERFOOT_API_KEY")
            created.raise_for_status()
            scan_id = created.json().get("id")
            if not scan_id:
                raise ValueError("SpiderFoot did not return a scan id")

            while True:
                if cancelled is not None and cancelled.is_set():
                    raise LostLease("spiderfoot scan cancelled or lease lost")
                if time.time() > deadline:
                    raise ValueError(f"spiderfoot exceeded {timeout:g}s")
                status = client.get(f"/api/v1/scans/{scan_id}")
                status.raise_for_status()
                state = (status.json().get("status") or "").upper()
                if state == "FINISHED":
                    break
                # Anything else terminal is a failed scan: a partial result set would
                # present an incomplete scan as a finished one.
                if state in {"ERROR-FAILED", "ABORTED", "ABORT-REQUESTED"}:
                    raise ValueError(f"spiderfoot scan ended as {state}")
                time.sleep(2)

            records: list[dict] = []
            page = 1
            while True:
                got = client.get(
                    f"/api/v1/scans/{scan_id}/events",
                    params={"page": page, "page_size": 500},
                )
                got.raise_for_status()
                payload = got.json()
                events = payload.get("events") or []
                records.extend(events)
                if not payload.get("has_next") or not events:
                    break
                page += 1
            return records
        except httpx.HTTPError as exc:
            # Never surface the response body: it can echo the request headers.
            raise ValueError(f"spiderfoot request failed: {type(exc).__name__}") from None
        finally:
            if scan_id and cancelled is not None and cancelled.is_set():
                try:
                    client.delete(f"/api/v1/scans/{scan_id}")
                except httpx.HTTPError:
                    log.warning("could not stop spiderfoot scan after cancellation")


# kinds: the planning investigation types whose normalized value is a valid target.
TOOLS: dict[str, dict] = {
    "maigret": {"kinds": ("username",), "run": _maigret},
    "spiderfoot": {"kinds": ("domain", "organization"), "run": _spiderfoot},
}


def run(name: str, target: str, settings, cancelled: threading.Event | None = None) -> Capture:
    """Run one allowlisted tool and return its output as an immutable capture."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"unknown tool {name!r}")
    if name not in settings.tools:
        raise PolicyDenied(f"tool {name!r} is not enabled; add it to HARVEST_TOOLS")
    if not _TARGET_RE.fullmatch(target):
        raise ValueError(
            "tool target must start with a letter, digit or underscore and contain only "
            "letters, digits and _ . @ + -"
        )
    started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"harvest-{name}-") as workdir:
        # Every tool takes the same arguments and reads what it needs off settings,
        # so adding one does not mean another branch here.
        records = tool["run"](target, workdir, settings.tool_timeout, cancelled, settings)
    body = json.dumps({"tool": name, "target": target, "results": records}).encode()
    url = f"tool://{name}/{target}"
    return Capture(
        url=url,
        final_url=url,
        status=200,
        headers={"content-type": "application/json"},
        body=body,
        retrieved=started,
    )
