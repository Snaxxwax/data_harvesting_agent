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

import functools
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

from .extract import http_url
from .models import TOOL_TARGET_PATTERN, LostLease, PolicyDenied
from .network import Capture

log = logging.getLogger("harvest")

# ToolRun applies this at submit; re-checked here because tools.run is also called
# directly. fullmatch, not match: "$" would otherwise accept a trailing newline, letting
# "alice\n" through into an argv element, a report filename and a capture URL.
_TARGET_RE = re.compile(TOOL_TARGET_PATTERN)


def _tool_env(proxy: str | None, inject: bool = False) -> dict[str, str]:
    """Remove ambient proxies; Maigret receives the configured route via --proxy.

    `inject` puts the validated proxy back as HTTP(S)_PROXY instead, for a tool that
    offers no proxy flag at all. GHunt builds its httpx client without one and leaves
    trust_env at its default, so the environment is the only route it reads. Stripping
    first and re-adding the validated value (rather than passing os.environ through)
    keeps an unvalidated ambient proxy from reaching any tool.
    """
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
    if inject:
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = env["https_proxy"] = proxy
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


def _assert_spiderfoot_proxied(settings) -> None:
    """Refuse a scan unless the deployment declares SpiderFoot's own egress is proxied.

    SpiderFoot scans from its own container, so neither Harvest's proxy nor its egress probe
    covers a single module request. This used to read SpiderFoot's `_socks*` global proxy
    back over `GET /api/v1/config` and compare it to HARVEST_EGRESS_PROXY. That check was
    worse than no check, for two independent reasons measured on SpiderFoot NG 6.1.0:

      * It never passed. `save_config()` DOES persist `_socks*` to Postgres (configSerialize
        filters `__`-prefixed keys, not `_`-prefixed ones -- the rows are there), but nothing
        reloads them at startup, so the endpoint always answers with the hardcoded "" default.
        With two uvicorn workers it is also nondeterministic: a read immediately after a
        PATCH answers from whichever worker serves it, so the same value read twice differed.
        The practical effect was that `spiderfoot` was permanently policy-denied -- the tool
        had not run once since proxy-only mode was enabled.
      * Had it passed, it would have proved nothing. The scanner ignores that setting. A scan
        whose modules reached their targets moved 0 bytes through the relay, i.e. it egressed
        directly from this host while the API happily reported a configured proxy.

    So the value being asserted had no causal relationship to where the packets went.
    What actually works is the standard `HTTP(S)_PROXY` environment on the scanner container
    (its modules use `requests`, which honours it): measured exit IP becomes the upstream
    proxy's instead of this host's. That is a container-level property, and this process
    cannot read another container's environment -- so it is declared here and *verified* by
    verify-deployment.sh, which has Docker access and checks the env var, the network
    attachment, and the scanner's real exit IP.

    Fail-closed: the default is "direct", so a deployment that has not opted in is refused
    rather than silently scanning from this host's address.
    """
    declared = getattr(settings, "spiderfoot_egress", "direct")
    if declared == "proxy-env":
        return
    raise PolicyDenied(
        "HARVEST_EGRESS_MODE=proxy but HARVEST_SPIDERFOOT_EGRESS is "
        f"{declared!r}; SpiderFoot's modules run in their own container and would egress "
        "directly. Give the scanner container HTTP_PROXY/HTTPS_PROXY pointing at "
        "HARVEST_EGRESS_PROXY, attach it to the egress network, then set "
        "HARVEST_SPIDERFOOT_EGRESS=proxy-env. Do not rely on SpiderFoot's own _socks* "
        "config: it is not reloaded at startup and its scanner ignores it."
    )


def _maigret_confidence(site: dict) -> float:
    """How strong one "Claimed" verdict actually is.

    Maigret reports the same word for very different evidence: a page whose profile data it
    parsed, a page containing an expected string, and a URL that merely returned 200. They
    were all landing as confidence 1.0, which is how a login-required 403 page ended up in a
    dossier as a claimed account. The numbers are deliberately coarse -- they rank evidence,
    they do not estimate a probability.
    """
    status = site.get("status") or {}
    http = site.get("http_status")
    if isinstance(http, int) and not isinstance(http, bool) and not 200 <= http < 300:
        # The verdict came off a non-success response: an error, login wall or block page
        # that happened to contain one of the site definition's "presence" strings.
        return 0.3
    if status.get("ids"):
        return 0.9  # profile data was parsed off the page, not merely its existence
    check = str((site.get("site") or {}).get("checkType") or "").lower()
    base = {"message": 0.6, "response_url": 0.5, "status_code": 0.4}.get(check, 0.4)
    # A "similar" hit is maigret's own flag that the handle is not the one searched for.
    return round(base - 0.1, 2) if site.get("is_similar") else base


def _maigret_record(name: str, site: dict, target: str) -> dict:
    """One found account as a flat record of individually-sourced fields.

    Three things are deliberately dropped. `site` is maigret's own site *definition* -- url
    templates, regexes and the `usernameClaimed`/`usernameUnclaimed` probe sentinels -- which
    is configuration, not evidence about the subject; emitting it turned "blue" and
    "noonewouldeverusethis7" into claims and sent unexpanded `{username}` templates to the
    crawler. `url_main` is the site's front page, which is a navigation page rather than
    profile enrichment. The nested `status` blob is flattened, because `status.ids` is where
    the genuinely useful evidence (fullname, account id, creation date) was buried -- as one
    opaque JSON value it could not be queried, mapped to a dossier field, or corroborated
    against another source.
    """
    status = site.get("status") or {}
    ids = status.get("ids") if isinstance(status.get("ids"), dict) else {}
    confidence = _maigret_confidence(site)
    record = {
        "sitename": name,
        "username": site.get("username"),
        # Two axes, same meaning as SpiderFoot's. `existence`: whether maigret actually read a
        # profile (strong) or only saw a status code / a "similar" near-miss (weak). `ownership`
        # is ALWAYS "candidate": a handle existing on a site is evidence the account exists, never
        # evidence that the person under investigation owns it. The dossier keeps the identity
        # decision out of band.
        "existence": "observed" if confidence >= 0.5 else "inferred",
        "ownership": "candidate",
        # The profile URL. Also the entity key (record_key prefers "url") and the one lead
        # worth following for enrichment.
        "url": site.get("url_user"),
        "url_probe": site.get("url_probe"),
        "status": status.get("status"),
        "http_status": site.get("http_status"),
        "is_similar": site.get("is_similar"),
        # maigret uses sys.maxsize as "unranked". Recorded verbatim it is a 19-digit claim
        # that looks like real data; None drops it, which is what "unranked" means.
        "rank": None if site.get("rank") == 9223372036854775807 else site.get("rank"),
        "tags": status.get("tags") or None,
        "_confidence": confidence,
    }
    # Promote the parsed profile fields to top level under maigret's own names, so each is
    # its own claim with its own locator instead of one unqueryable blob. Keys starting with
    # "_" are the extractor's metadata, not observations.
    for key, value in ids.items():
        if key.startswith("_") or key in record:
            continue
        record[str(key)] = value
    # ids_usernames echoes handles found on the page. The target's own handle is the input,
    # not a finding: as a claim it reads as the account corroborating itself.
    others = {
        handle: kind
        for handle, kind in (site.get("ids_usernames") or {}).items()
        if isinstance(handle, str) and handle.casefold() != target.casefold()
    }
    if others:
        record["ids_usernames"] = others
    if site.get("ids_links"):
        record["ids_links"] = site["ids_links"]
    return {k: v for k, v in record.items() if v is not None}


def _merge_subdomain_duplicates(records: list[dict]) -> list[dict]:
    """Collapse one account reported once per host in the same domain family.

    Maigret's database holds separate entries for a forum reachable at both its apex and a
    subdomain (antichat.io and forum.antichat.io share one vBulletin definition), so a single
    account is reported as two claimed accounts and corroborates itself in the dossier. Two
    records merge only when their profile paths and queries are identical AND one host is a
    suffix of the other, which is exact rather than a guess at a registrable domain -- it can
    never merge two unrelated sites. The strongest record survives and names the others, so
    the merge removes a duplicate entity without discarding the evidence.
    """
    groups: list[list[dict]] = []
    for record in records:
        parts = urlsplit(record.get("url") or "")
        host, shape = (parts.hostname or ""), (parts.path, parts.query)
        for group in groups:
            other = urlsplit(group[0]["url"])
            other_host = other.hostname or ""
            if (other.path, other.query) == shape and (
                host == other_host
                or host.endswith("." + other_host)
                or other_host.endswith("." + host)
            ):
                group.append(record)
                break
        else:
            groups.append([record])
    merged = []
    for group in groups:
        if len(group) == 1:
            merged.append(group[0])
            continue
        # Strongest evidence wins; the shortest host breaks a tie, preferring the apex.
        keep = min(group, key=lambda r: (-r["_confidence"], len(urlsplit(r["url"]).hostname or "")))
        best = dict(keep)
        # The merged-away hosts stay on the record as a claim: the duplicate entity is gone,
        # the evidence that maigret saw the account at those hosts is not.
        best["related_sites"] = sorted(
            f"{r['sitename']}: {r['url']}" for r in group if r is not keep
        )
        merged.append(best)
    return merged


def _maigret(
    target: str,
    workdir: str,
    timeout: float,
    cancelled: threading.Event | None = None,
    settings=None,
    top_sites: int | None = None,
) -> list[dict]:
    """`--json simple` writes report_<username>_simple.json: an object keyed by sitename,
    holding only CLAIMED accounts. Flattened to a list so the JSON adapter yields one entity
    per account rather than one entity with 500 nested fields, and `url_user` is surfaced as
    `url` so each found profile becomes both a stable entity key and a crawlable lead.

    See `_maigret_record` for what each record keeps and drops, and `_maigret_confidence`
    for why they do not all arrive as confidence 1.0.
    """
    argv = [
        "maigret",
        target,
        # All known sites rather than the top-ranked default: roughly ten times the sites,
        # so roughly ten times the outbound requests, none of which pass through the
        # fetcher's budgets. Measured at 309 MiB peak against the worker's 512 MB limit.
        # Some site definitions in the full set raise internally without changing the exit
        # code, so a clean exit with a report remains the success signal. ToolRun.top_sites
        # trades that breadth for a smaller scan when a run does not need all of it.
        *(["--top-sites", str(top_sites)] if top_sites else ["--all-sites"]),
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
    return _merge_subdomain_duplicates(
        [
            _maigret_record(name, site, target)
            for name, site in data.items()
            if isinstance(site, dict) and site.get("url_user")
        ]
    )


# GHunt's PROFILE container, per its own parsers (ghunt/parsers/people.py). Each of these
# lives two or three levels down, keyed by the Google "container" name, so as raw JSON they
# are one opaque claim that no dossier field_map can address -- the same defect maigret's
# status.ids had. Mapped to flat names on the record instead. Values are promoted only when
# they are the expected scalar type, so a schema change degrades to "field absent", which is
# today's behaviour, rather than to a wrong claim.
#
# THE THREE `names` FIELDS BELOW CAN NEVER POPULATE, and that is upstream's doing, not a
# bug here. As of 2.3.4 `PersonName._scrape` is a bare `pass` whose comment reads "Google
# patched the names :/ very sad" -- the displayName/givenName/familyName reads are commented
# out -- so `fullname`, `firstName` and `lastName` keep their `""` initialisers for every
# account. The exact-type-plus-nonempty check below correctly degrades them to "field
# absent" rather than asserting an empty name, so a dossier that maps them will list them in
# its per-target `missing_fields` on every single run. That is the accurate report, not a
# regression to chase. They are kept here, not deleted, so that a GHunt release which
# restores name scraping starts populating them with no change on this side.
_GHUNT_PROFILE_FIELDS = (
    ("names", "fullname", "fullname", str),
    ("names", "firstName", "first_name", str),
    ("names", "lastName", "last_name", str),
    ("emails", "value", "email_profile", str),
    ("profilePhotos", "url", "image_url", str),
    ("profilePhotos", "isDefault", "profile_photo_is_default", bool),
    ("coverPhotos", "url", "cover_image_url", str),
)


def _ghunt_profile(profile: dict, container: str = "PROFILE") -> dict:
    """Flatten GHunt's nested PROFILE container into queryable top-level fields.

    GHunt keys every sub-object by container name and always requires a "PROFILE" one (its
    email module exits when that is absent), so that is the container read here. Anything
    not promoted stays on the record untouched, so no evidence is lost.
    """
    flat: dict = {}
    for section, source, target, expected in _GHUNT_PROFILE_FIELDS:
        node = profile.get(section)
        if not isinstance(node, dict):
            continue
        entry = node.get(container)
        if not isinstance(entry, dict):
            continue
        value = entry.get(source)
        # bool is an int subclass, so an exact type check keeps a boolean out of a str field
        # and a stray 0/1 out of a boolean one.
        if type(value) is expected and (value != "" if expected is str else True):
            flat[target] = value
    types = (profile.get("profileInfos") or {}).get(container)
    if isinstance(types, dict) and isinstance(types.get("userTypes"), list):
        kinds = [t for t in types["userTypes"] if isinstance(t, str)]
        if kinds:
            # Distinguishes a consumer account from a Workspace one, which is the closest
            # thing GHunt reports to "what kind of account is this".
            flat["user_types"] = kinds
    return flat


def _ghunt(
    target: str,
    workdir: str,
    timeout: float,
    cancelled: threading.Event | None = None,
    settings=None,
) -> list[dict]:
    """`ghunt email <addr> --json <file>` writes one object per account container.

    Returned as one record per container (normally just PROFILE_CONTAINER) so the JSON
    adapter yields one entity for the Google account rather than one entity per nested
    section. `personId` is surfaced as `id` because record_key would otherwise fall back to
    a content fingerprint, which would mint a new entity every time any profile detail
    changed; the Gaia ID keeps the account's identity stable across reruns.
    """
    # GHunt derives its credential path from Path.home() with no override, so the creds
    # must exist under the worker's own HOME. Checked before launch because `ghunt email`
    # on missing creds exits asking for interactive login, which would read as a scan
    # failure rather than a configuration one.
    creds = Path(os.environ.get("HOME", "")) / ".malfrats" / "ghunt" / "creds.m"
    if not creds.is_file():
        raise PolicyDenied(
            f"ghunt has no credentials at {creds}; it requires an authenticated Google "
            "account. Run `ghunt login` inside this worker, with HOME on persistent "
            "storage so the session survives a restart"
        )
    report = Path(workdir) / "ghunt.json"
    argv = ["ghunt", "email", target, "--json", str(report)]
    proxy = getattr(settings, "proxy", None)
    proxy_only = bool(getattr(settings, "proxy_only", False))
    # Unlike Maigret there is no --proxy flag: GHunt's get_httpx_client() takes no proxy
    # argument, so the environment is the only route. That makes the env the whole of the
    # application-side guarantee, which is weaker than a flag, so proxy-only mode still
    # requires the host to block direct egress and verifies it rather than assuming it.
    env = _tool_env(proxy, inject=bool(proxy))
    if proxy_only:
        _assert_no_direct_egress(settings)
    elif proxy:
        log.warning("ghunt is proxied only through HTTP(S)_PROXY, which it may ignore")
    _exec(argv, timeout, workdir, cancelled, env=env)
    if not report.exists():
        raise ValueError("ghunt exited cleanly but wrote no JSON report")
    data = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ghunt JSON report was not an object keyed by container")
    records = []
    for name, container in data.items():
        if not isinstance(container, dict):
            continue
        profile = container.get("profile")
        profile = profile if isinstance(profile, dict) else {}
        person = profile.get("personId")
        records.append(
            {
                "container": name,
                "email": target,
                # GHunt resolves the Google account OF the exact address queried, so this is
                # not a handle coincidence: `ownership: self` means "the account belonging to
                # this identifier", distinct from maigret/SpiderFoot's "candidate". It still is
                # not a claim about the PERSON -- whoever controls the address controls the
                # account -- which the dossier states rather than inferring identity.
                "existence": "observed",
                "ownership": "self",
                **({"id": person} if isinstance(person, str) and person else {}),
                # The tool's own structure first, then the promoted scalars, so a promotion
                # always lands even if GHunt later adds a container key of the same name.
                # Nothing is dropped: `profile` is still here whole, nested, as evidence.
                **container,
                **_ghunt_profile(profile),
            }
        )
    return records


# --- Versioned attribution, applied when a tool capture is (re-)extracted ----------------
#
# A capture's body is immutable evidence, so a capture taken before the existence/ownership
# axes existed (maigret capture 212, the early ghunt/spiderfoot runs) can never gain them by
# editing it. Instead the CURRENT attribution rules are applied each time a tool capture is
# extracted: a replay of an old capture is a new, offline extraction revision whose ownership
# claims follow today's rules, while the original capture, its original extraction and every
# earlier observation stay exactly as they were. Each field this changes is marked derived
# (observation method "derived:<revision>", see extract.records_extraction) and a value the
# capture itself carried is kept beside it as `<field>_as_captured` -- so a reader can always
# tell what the tool said from what Harvest concluded, and which rule revision concluded it.
ATTRIBUTION_REVISION = "attribution/2"


def attribute_record(tool: str, record: dict, target: str) -> dict:
    """`record` with existence/ownership set by the current rules for `tool`.

    Never "confirmed": no tool output establishes that a person owns an account.
    """
    if tool == "maigret":
        existence = record.get("existence") or (
            "observed" if _maigret_confidence(record) >= 0.5 else "inferred"
        )
        ownership = "candidate"
    elif tool == "spiderfoot":
        existence = _spiderfoot_existence(
            str(record.get("event_type") or ""), str(record.get("module") or "")
        )
        ownership = _spiderfoot_ownership(existence, record.get("derived_via"))
    elif tool == "ghunt":
        existence, ownership = "observed", "self"
    else:
        return record
    out, derived = dict(record), {}
    for field, value in (("existence", existence), ("ownership", ownership)):
        captured = record.get(field)
        if captured == value:
            continue
        if captured is not None:
            out[f"{field}_as_captured"] = captured
        out[field] = value
        derived[field] = ATTRIBUTION_REVISION
    # A pre-grading maigret capture claimed every account at confidence 1.0; grade it the same
    # way a live capture is graded today. Metadata, not a claim, so it is not marked derived.
    if tool == "maigret" and "_confidence" not in record:
        out["_confidence"] = _maigret_confidence(record)
    if derived:
        out["_derived"] = derived
    return out


def _spiderfoot_target_type(target: str) -> str:
    """The explicit SpiderFoot target type for a Harvest target.

    Harvest always sends this to the fork's `ScanRequest.target_type`, which decouples the
    type signal from the value and so removes the bug that made a bare username unscannable:
    the API used to infer the type from the string and reuse the string as the value, typing
    an unquoted handle INTERNET_NAME (no identity module consumes it) and carrying a quoted
    one's quotes into every module URL. With an explicit type the stripped value reaches the
    modules clean. An older SpiderFoot that lacks the field simply ignores it (Pydantic drops
    an unknown field), so this is safe to send to either.

    Shape test, not a resolver call: an "@" is an address, a dotted label is a hostname, and
    anything else is a username -- which SpiderFoot can now scan.
    """
    if "@" in target:
        return "EMAILADDR"
    if "." in target:
        return "INTERNET_NAME"
    return "USERNAME"


# One scan can emit tens of thousands of events. Pagination stops here rather than holding
# an unbounded list in memory; dedup below usually collapses the kept set far below this.
# ponytail: a flat cap, not a per-type quota -- add one only if a real scan starves a type.
_SPIDERFOOT_MAX_EVENTS = 5000

# SpiderFoot event types that are NOT an observation about the target, whatever module
# emitted them. The prefixes are SpiderFoot's own vocabulary for "related to, but not, the
# thing you asked about": an AFFILIATE_ is someone else's address on the same infrastructure,
# a SIMILAR_ is explicitly a near-miss handle, a CO_HOSTED_ is a neighbour on shared hosting.
# Promoting any of these to a confirmed identity is how an unrelated person ends up in a
# dossier, so they are kept -- they are useful leads -- and labelled.
_SF_CANDIDATE_PREFIXES = ("AFFILIATE_", "SIMILAR_", "CO_HOSTED_", "DARKNET_", "LEAKSITE_")

# Modules whose output is derived rather than observed. sfp_names infers human names from the
# local part of an address ("errlybird49" -> plausible first/last names); that is a guess
# about a person, and it is exactly the kind of guess that must never read as confirmed.
_SF_CANDIDATE_MODULES = frozenset({"sfp_names"})

# Event types that carry no claim about the subject: the scan's own input, echoed back.
_SF_NOISE_TYPES = frozenset({"ROOT"})


# SpiderFoot does not put a bare URL in `data`. Its highest-value identity events wrap one
# in an <SFURL> tag on a second line, with a human label on the first:
#
#   ACCOUNT_EXTERNAL_OWNED  "Pinterest (Category: social)\n<SFURL>https://pinterest.com/x/</SFURL>"
#
# Treating that whole string as the value cost two things at once: `http_url()` rejected it,
# so the account produced NO follow-up lead, and with no `url` the record fell back to a
# content fingerprint for its entity key -- minting a new entity whenever the label changed.
_SFURL_RE = re.compile(r"<SFURL>\s*(.*?)\s*</SFURL>", re.DOTALL | re.IGNORECASE)


def _spiderfoot_split_data(data: str) -> tuple[str, str | None]:
    """Split one event's `data` into its label and its URL, if it carries one."""
    match = _SFURL_RE.search(data)
    if not match:
        return data, http_url(data)
    link = http_url(match.group(1))
    label = _SFURL_RE.sub("", data).strip()
    # A bare <SFURL> with no label is the URL itself; never return an empty value.
    return (label or link or data), link


def _spiderfoot_existence(event_type: str, module: str) -> str:
    """Did SpiderFoot OBSERVE this, or INFER it?

    "observed" is a real finding about the identifier it consumed -- a profile it fetched, a
    breach record, a resolved address. "inferred" is SpiderFoot's own "related to, but not,
    the thing you asked about" (AFFILIATE_/SIMILAR_/CO_HOSTED_/...) or a guess derived from
    the input (sfp_names turns an address local part into plausible human names). This axis
    is only about whether the thing exists/was seen -- NOT about who owns it.
    """
    if module in _SF_CANDIDATE_MODULES or event_type.startswith(_SF_CANDIDATE_PREFIXES):
        return "inferred"
    return "observed"


def _spiderfoot_ownership(existence: str, derived_via: str | None) -> str:
    """Does this finding belong to the investigation TARGET? A separate question from whether
    it exists, and one a tool cannot answer "confirmed".

      * "candidate" -- the link to the person is a handle/name coincidence: the finding was
        reached by deriving an identifier from the target (an account hanging off a USERNAME
        that sfp_accounts built from an email's local part), or it is itself an inferred
        near-miss/name. A handle matching is NOT proof the target owns the account.
      * "unverified" -- a direct observation about the target's own identifier (e.g. a breach
        record for the exact address), which still is not proof the target controls it.

    Deliberately never "confirmed": nothing SpiderFoot returns establishes ownership, so the
    record must not carry a field that reads as if it had. The dossier keeps identity
    decisions out of band and reversible.
    """
    if existence == "inferred" or derived_via:
        return "candidate"
    return "unverified"


def _spiderfoot_confidence(event: dict, existence: str) -> float:
    """SpiderFoot's own 0-100 confidence, rescaled, with inferred findings held below observed.

    The ceiling on inferred findings is the point: without it an inferred HUMAN_NAME arrives
    at SpiderFoot's default confidence of 100 and outranks a profile that was actually read.
    This scores EXISTENCE strength only; it says nothing about ownership. Coarse on purpose --
    it ranks evidence, it does not estimate a probability.
    """
    raw = event.get("confidence")
    value = raw / 100 if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 1.0
    value = max(0.0, min(1.0, value))
    return round(min(value, 0.5), 2) if existence == "inferred" else round(value, 2)


def _spiderfoot_records(events: list, target: str) -> list[dict]:
    """Flatten SpiderFoot events into deduplicated, queryable records.

    Raw events were being returned verbatim, which had the same defect maigret's `status.ids`
    and GHunt's nested containers had: the useful part was addressable only as opaque JSON,
    so no dossier field_map could reach it. Each event becomes flat fields instead.

    Three things are dropped. ROOT is the scan's own input. The echo of the target itself
    (same value as the target, by any module) is the input restated, not a finding. And an
    event with no `data` has nothing to claim.

    Deduplication is by (type, data): a dozen modules reporting the same address is one fact
    found a dozen ways, not a dozen facts. The strongest record survives and names the other
    modules in `related_modules`, so collapsing the duplicate entity never discards the
    evidence that another module saw it too -- the same shape as `_merge_maigret_hosts`.
    """
    normalized = (target or "").strip().lower()
    # hash -> type, so an event can name what it was derived from. Built first because
    # SpiderFoot does not order events parent-before-child.
    lineage = {
        e.get("hash"): (str(e.get("type") or ""), e.get("data"))
        for e in events
        if isinstance(e, dict) and e.get("hash")
    }
    groups: dict[tuple, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        data = event.get("data")
        if event_type in _SF_NOISE_TYPES or not event_type:
            continue
        if not isinstance(data, str) or not data.strip():
            continue
        data = data.strip()
        if data.lower() == normalized:
            continue  # the scan's own target, echoed back as an event
        module = str(event.get("module") or "").strip()
        existence = _spiderfoot_existence(event_type, module)
        data, link = _spiderfoot_split_data(data)
        # derived_via is computed just below; ownership needs it, so look up the parent now.
        parent_type, parent_data = lineage.get(event.get("source_event_hash"), ("", None))
        derived_via = (
            parent_type
            if parent_type and parent_type not in _SF_NOISE_TYPES and parent_type != event_type
            else None
        )
        record = {
            "event_type": event_type,
            "data": data,
            "module": module or None,
            # Two independent axes, flat and queryable so a dossier can map either and a reader
            # never needs SpiderFoot's type vocabulary. `existence`: did we see it or infer it.
            # `ownership`: does it belong to the target -- never "confirmed" from a tool, because
            # an account reached by a matching handle is not proof the target owns it.
            "existence": existence,
            "ownership": _spiderfoot_ownership(existence, derived_via),
            "risk": event.get("risk"),
            "visibility": event.get("visibility"),
            "generated": event.get("generated"),
            "_confidence": _spiderfoot_confidence(event, existence),
        }
        # A URL-valued event is the one lead worth following. `url` is also what record_key
        # prefers, so these keep a stable entity across reruns instead of a content hash.
        if link:
            record["url"] = link
        # Provenance: which finding this one came OFF. SpiderFoot chains events by hash, and
        # for an email target the chain is the whole story -- sfp_accounts derives a USERNAME
        # from the local part and the accounts hang off THAT, so an account reported for an
        # address was reached by handle, not by anything tying the address to the profile.
        # This is why `ownership` above is "candidate" whenever derived_via is set: it records
        # what the attribution rests on, which `existence` and `ownership` summarise.
        if derived_via:
            record["derived_via"] = derived_via
            # The parent's VALUE, not just its type, because that is the only field on an
            # account record that a dossier can match an identifier against. An account's
            # own `data` is a label ("Pinterest (Category: social)"), so without this the
            # accounts land in the dossier's `unresolved` list -- found, evidenced, and
            # attached to nobody -- while only the derived USERNAME itself binds.
            if isinstance(parent_data, str) and parent_data.strip():
                record["derived_from"] = parent_data.strip()
        # Keyed on the URL when there is one: the same profile reported with two different
        # labels is one account, and two accounts could share a label.
        key = (event_type, link or data)
        existing = groups.get(key)
        if existing is None:
            groups[key] = record
            continue
        # Same fact, another module. Keep the stronger record; remember both modules.
        strongest, weaker = (
            (existing, record)
            if existing["_confidence"] >= record["_confidence"]
            else (record, existing)
        )
        seen = set(strongest.get("related_modules") or [])
        seen.update(weaker.get("related_modules") or [])
        if weaker.get("module"):
            seen.add(weaker["module"])
        strongest["related_modules"] = sorted(seen - {strongest.get("module")})
        groups[key] = strongest
    return list(groups.values())


# --- Per-input SpiderFoot module selection -------------------------------------------------
#
# HARVEST_SPIDERFOOT_MODULES is the operator's ALLOWLIST, not the scan. Sending all of it for
# every target ran email modules against a domain and DNS modules against a handle: wasted
# requests at best, and a module set nobody could predict from the input at worst. The scan
# now gets the subset that can actually fire for this target, computed from each module's
# own declared watched/produced event types (vendored from the deployed fork, below).

_SF_META_PATH = Path(__file__).with_name("spiderfoot_modules.json")


@functools.cache
def _sf_meta() -> dict[str, dict]:
    return json.loads(_SF_META_PATH.read_text())["modules"]


# What each module has actually done on the deployed fork, per SpiderFoot target type, read
# from its scan history (tbl_scan_results, 2026-10-02..04) and the live demonstration that
# accompanies this table. "events": produced findings. "ran-empty": was enabled and ran on
# that type, producing nothing for the authorized test targets (not proof it is broken).
# Absent: never exercised on that type. Reported with every plan, never used to hide a
# module -- except that a keyed module observed producing events WITHOUT a configured key
# is evidently usable keyless, which the credential gate below honours.
_SF_TESTED: dict[tuple[str, str], str] = {
    ("sfp_accounts", "EMAILADDR"): "events",
    ("sfp_tiktok_osint", "EMAILADDR"): "events",
    ("sfp_gravatar", "EMAILADDR"): "ran-empty",
    ("sfp_hudsonrock", "EMAILADDR"): "ran-empty",
    ("sfp_pgp", "EMAILADDR"): "ran-empty",
    ("sfp_debounce", "EMAILADDR"): "ran-empty",
    ("sfp_names", "EMAILADDR"): "ran-empty",
    ("sfp_wikileaks", "EMAILADDR"): "ran-empty",
    ("sfp_dnsresolve", "INTERNET_NAME"): "events",
}

# Identifier event types per kind of subject; see spiderfoot_plan for why only these are followed.
_SF_SUBJECT_KINDS = (
    frozenset({"EMAILADDR", "USERNAME", "PHONE_NUMBER"}),
    frozenset({"INTERNET_NAME", "DOMAIN_NAME", "IP_ADDRESS", "IPV6_ADDRESS"}),
)

# Harvest investigation kind -> the SpiderFoot target type _spiderfoot_target_type gives it.
SPIDERFOOT_KIND_TYPES = {
    "email": "EMAILADDR",
    "username": "USERNAME",
    "domain": "INTERNET_NAME",
    "organization": "INTERNET_NAME",
}


def spiderfoot_plan(target_type: str, settings) -> dict:
    """The modules a scan of this target type will actually run, and why each other is not.

    Dependency-aware: a module is selected when it watches an event type reachable from the
    target -- the target itself, or an identifier an already-selected module produces -- so on
    a domain, sfp_dnsresolve's DOMAIN_NAME is what lets a DOMAIN_NAME-only module fire.

    Reachability follows only identifiers of the SAME KIND of subject as the target: person
    identifiers (address, handle, phone) for a person target, host identifiers for a host
    target. Everything else is evidence, not a reason to run more modules: raw blobs
    (RAW_RIR_DATA, page content) contain incidental identifiers of anyone; a HUMAN_NAME is
    never unique; the "related, but not the target" events (AFFILIATE_/SIMILAR_/CO_HOSTED_)
    are a neighbour's; and a domain's addresses are other people. Following any of them is
    how a scan wanders from the authorized target into unrelated people -- with them
    followed, every allowlisted module was reachable from every input, which selected nothing.

    Pure function of Settings and the vendored metadata, so `/meta`, the planner and the scan
    itself report the same set.
    """
    meta = _sf_meta()
    keyed = getattr(settings, "spiderfoot_keyed_modules", frozenset())
    keyless_ok = {m for (m, _), outcome in _SF_TESTED.items() if outcome == "events"}
    excluded: dict[str, str] = {}
    usable: dict[str, dict] = {}
    for module in getattr(settings, "spiderfoot_modules", ()) or ():
        info = meta.get(module)
        if info is None:
            excluded[module] = "unknown to this build's SpiderFoot module metadata; not sent"
        elif "apikey" in info["flags"] and module not in keyed and module not in keyless_ok:
            excluded[module] = (
                "needs an API key not declared configured (HARVEST_SPIDERFOOT_KEYED_MODULES)"
            )
        else:
            usable[module] = info
    follow = next((kind for kind in _SF_SUBJECT_KINDS if target_type in kind), frozenset())
    reachable, selected = {target_type}, {}
    changed = True
    while changed:
        changed = False
        for module, info in usable.items():
            hit = module not in selected and reachable.intersection(info["watched"])
            if hit:
                selected[module] = sorted(hit)
                reachable.update(e for e in info["produced"] if e in follow)
                changed = True
    for module in usable:
        if module not in selected:
            excluded[module] = f"consumes nothing reachable from the {target_type} target"
    # Disclose what the fork could do for this input that the allowlist does not enable.
    allowed = set(getattr(settings, "spiderfoot_modules", ()) or ())
    not_enabled = sorted(
        m for m, info in meta.items() if target_type in info["watched"] and m not in allowed
    )
    return {
        "target_type": target_type,
        "modules": sorted(selected),
        "consumes": selected,
        "excluded": excluded,
        "tested": {m: _SF_TESTED.get((m, target_type), "untested") for m in sorted(selected)},
        "not_enabled": not_enabled,
    }


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
    if not base:
        raise PolicyDenied("HARVEST_SPIDERFOOT_URL is not configured")
    if not key:
        raise PolicyDenied("HARVEST_SPIDERFOOT_API_KEY is not configured")
    if not getattr(settings, "spiderfoot_modules", ()):
        raise PolicyDenied("HARVEST_SPIDERFOOT_MODULES is empty; no scan would run")
    target_type = _spiderfoot_target_type(target)
    modules = spiderfoot_plan(target_type, settings)["modules"]
    if not modules:
        # Refuse rather than launch a scan that can only echo its own target back.
        raise PolicyDenied(
            f"no allowlisted SpiderFoot module consumes a {target_type} target; "
            "see /meta capabilities.spiderfoot.plans"
        )

    # The key authenticates every call; it must never reach a log or a persisted error.
    headers = {"X-API-Key": key, "content-type": "application/json"}
    deadline = time.time() + timeout
    scan_id = None
    with httpx.Client(base_url=base, headers=headers, timeout=30.0) as client:
        try:
            if getattr(settings, "proxy_only", False):
                _assert_spiderfoot_proxied(settings)
            created = client.post(
                "/api/v1/scans",
                json={
                    "name": f"harvest-{target}",
                    "target": target,
                    "target_type": target_type,
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
                if len(records) >= _SPIDERFOOT_MAX_EVENTS:
                    log.warning(
                        "spiderfoot scan produced >= %d events; stopping pagination",
                        _SPIDERFOOT_MAX_EVENTS,
                    )
                    break
                page += 1
            return _spiderfoot_records(records, target)
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
    "ghunt": {"kinds": ("email",), "run": _ghunt},
    "spiderfoot": {"kinds": tuple(SPIDERFOOT_KIND_TYPES), "run": _spiderfoot},
}


def run(
    name: str,
    target: str,
    settings,
    cancelled: threading.Event | None = None,
    top_sites: int | None = None,
) -> Capture:
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
        # so adding one does not mean another branch here. `top_sites` is maigret's scan
        # breadth; a tool whose run function does not take it simply does not see it.
        kwargs = {"top_sites": top_sites} if top_sites and name == "maigret" else {}
        records = tool["run"](target, workdir, settings.tool_timeout, cancelled, settings, **kwargs)
    result = {"tool": name, "target": target, "results": records}
    if name == "spiderfoot":
        # The executed module set is part of the evidence: what a scan did NOT look for is
        # what makes "no findings" interpretable.
        result["modules"] = spiderfoot_plan(_spiderfoot_target_type(target), settings)
    body = json.dumps(result).encode()
    url = f"tool://{name}/{target}"
    return Capture(
        url=url,
        final_url=url,
        status=200,
        headers={"content-type": "application/json"},
        body=body,
        retrieved=started,
    )
