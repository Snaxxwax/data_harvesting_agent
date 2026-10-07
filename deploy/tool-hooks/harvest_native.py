"""Structured hooks for pinned tool builds. Never copy console streams or request secrets."""

import hashlib
import json
import os
import re


def emit(value):
    path = os.environ.get("HARVEST_CHECKPOINT")
    if not path:
        return
    # Closed structured contract: callers exclude HTTP bodies, cookies and request headers.
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps(value, ensure_ascii=False) + "\n")
        out.flush()
        os.fsync(out.fileno())


def maigret_plan(sites, database):
    emit(
        {
            "kind": "plan",
            "name": "eligible-sites",
            "native": {
                "sites": list(sites),
                "eligible": len(sites),
                "database_sha256": hashlib.sha256(open(database, "rb").read()).hexdigest(),
            },
        }
    )


def maigret_result(result):
    name, raw = result
    # Native structured fields only; exclude runtime objects, raw pages, headers and cookies.
    allowed = {
        "username",
        "url_user",
        "url_probe",
        "http_status",
        "is_similar",
        "rank",
        "ids_usernames",
        "ids_links",
        "status",
    }
    native = {k: v for k, v in raw.items() if k in allowed}
    status = raw.get("status")
    if status:
        native["status"] = status.json()
        error = native["status"].pop("error", None)
        state = str(native["status"].get("status", "")).lower()
    else:
        error, state = None, ""
    site = raw.get("site")
    native["site"] = {"checkType": site.json.get("checkType")} if site else {}
    code = native.get("http_status")
    if code in (401, 403, 407, 429) or (
        error and re.search("captcha|blocked|rate|cloudflare", str(error), re.I)
    ):
        outcome, reason = "blocked", "site challenge, authentication or rate limit"
    elif error or (isinstance(code, int) and code >= 500):
        outcome, reason = "errored", "native check failed"
    elif state == "claimed":
        outcome, reason = "found", ""
    elif state == "available":
        outcome, reason = "absent", ""
    elif state == "illegal":
        outcome, reason = "skipped", "identifier not eligible for site"
    else:
        outcome, reason = "unfinished", "no conclusive native verdict"
    emit({"tool": "maigret", "name": name, "outcome": outcome, "reason": reason, "native": native})


def ghunt_result(name, outcome, native=None, reason=""):
    if native is not None:
        from ghunt.objects.encoders import GHuntEncoder

        native = json.loads(json.dumps(native, cls=GHuntEncoder))
    emit({"tool": "ghunt", "name": name, "outcome": outcome, "native": native, "reason": reason})
