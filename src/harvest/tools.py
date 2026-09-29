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
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .models import PolicyDenied, RetryLater
from .network import Capture

log = logging.getLogger("harvest")

# The target reaches an external argv. A leading "-" would be read as a flag by any
# argparse-based tool, so the first character is restricted to alphanumerics: the value
# can never become an option, and no shell is ever involved (never shell=True).
_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,253}$")


def _exec(argv: list[str], timeout: float, cwd: str) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
            argv, capture_output=True, timeout=timeout, cwd=cwd, check=False
        )
    except FileNotFoundError as exc:
        raise PolicyDenied(f"{argv[0]} is not installed in this worker image") from exc
    except subprocess.TimeoutExpired as exc:
        raise RetryLater(f"{argv[0]} exceeded {timeout:g}s", delay=30) from exc
    if proc.returncode != 0:
        # stderr may carry a tool's own credentials (GHunt cookies); log it for the
        # operator, never return it into a persisted task error.
        log.warning(
            "tool_failed argv0=%s rc=%s stderr=%.500s", argv[0], proc.returncode, proc.stderr
        )
    return proc


def _maigret(target: str, workdir: str, timeout: float) -> list[dict]:
    """`--json simple` writes report_<username>_simple.json: an object keyed by sitename,
    holding only CLAIMED accounts. Flattened to a list so the JSON adapter yields one entity
    per account rather than one entity with 500 nested fields, and `url_user` is surfaced as
    `url` so each found profile becomes both a stable entity key and a crawlable lead.
    """
    argv = [
        "maigret",
        target,
        "--json",
        "simple",
        "--folderoutput",
        workdir,
        "--no-color",
        "--no-progressbar",
        "--timeout",
        str(max(1, int(min(timeout, 30)))),
    ]
    proc = _exec(argv, timeout, workdir)
    # maigret replaces "/" in the username when naming the report; _TARGET_RE already
    # rejects "/", so the name is the target verbatim.
    report = Path(workdir) / f"report_{target}_simple.json"
    if not report.exists():
        raise ValueError(f"maigret wrote no JSON report (exit {proc.returncode})")
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


def run(name: str, target: str, settings) -> Capture:
    """Run one allowlisted tool and return its output as an immutable capture."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ValueError(f"unknown tool {name!r}")
    if name not in settings.tools:
        raise PolicyDenied(f"tool {name!r} is not enabled; add it to HARVEST_TOOLS")
    if not _TARGET_RE.match(target):
        raise ValueError(
            "tool target must start with a letter, digit or underscore and contain only "
            "letters, digits and _ . @ + -"
        )
    started = time.time()
    with tempfile.TemporaryDirectory(prefix=f"harvest-{name}-") as workdir:
        records = tool["run"](target, workdir, settings.tool_timeout)
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
