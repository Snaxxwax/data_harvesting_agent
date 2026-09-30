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
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .models import TOOL_TARGET_PATTERN, LostLease, PolicyDenied
from .network import Capture

log = logging.getLogger("harvest")

# ToolRun applies this at submit; re-checked here because tools.run is also called
# directly. fullmatch, not match: "$" would otherwise accept a trailing newline, letting
# "alice\n" through into an argv element, a report filename and a capture URL.
_TARGET_RE = re.compile(TOOL_TARGET_PATTERN)


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
    argv: list[str], timeout: float, cwd: str, cancelled: threading.Event | None = None
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
                    stdout, stderr = proc.communicate(timeout=min(1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if proc.returncode != 0:
                    # stderr may carry a tool's own credentials (GHunt cookies); log it for
                    # the operator, never return it into a persisted task error.
                    log.warning(
                        "tool_failed argv0=%s rc=%s stderr=%.500s",
                        argv[0],
                        proc.returncode,
                        stderr,
                    )
                    # A tool that aborted may still have left a partial report behind;
                    # ingesting it would present an incomplete scan as a finished one. Every
                    # nonzero exit in maigret is a startup/config failure or an interrupt,
                    # never a per-site error.
                    raise ValueError(f"{argv[0]} exited {proc.returncode}")
                return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except FileNotFoundError as exc:
        raise PolicyDenied(f"{argv[0]} is not installed in this worker image") from exc


def _maigret(
    target: str, workdir: str, timeout: float, cancelled: threading.Event | None = None
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
    _exec(argv, timeout, workdir, cancelled)
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


# kinds: the planning investigation types whose normalized value is a valid target.
TOOLS: dict[str, dict] = {
    "maigret": {"kinds": ("username",), "run": _maigret},
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
        records = tool["run"](target, workdir, settings.tool_timeout, cancelled)
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
