#!/usr/bin/env python3
"""Score one investigation the same way regardless of who drove it.

Harvest, not the harness, owns evidence and attribution, so every run -- Harvest's own
deterministic policy or an LLM harness driving the MCP tools -- is scored by reading the SAME
investigation back through the SAME authenticated API. Content-free counts and ratios only.

Metrics, and what each one does NOT mean:

  citation_presence   fraction of observations that carry a capture id AND a locator. Says a
                      citation exists, not that it supports the value.
  evidence_support    fraction of checkable observations whose value is actually found in a
                      cited capture: at the locator for a JSON capture (the JSON pointer must
                      resolve to an equal value), or verbatim in the body otherwise. Rule-derived
                      claims (method "derived:...", e.g. ownership) are counted separately:
                      they are conclusions, not quotations, so "supported" does not apply.
                      Zero checkable observations -> "not_evaluated", never 1.0.
  attribution         against a labelled case (--labels): every identity association asserted
                      by Harvest (ownership "confirmed"/"self" observations) or by the harness's
                      own final answer (--answer: {"owned": [...]}) is scored as correct,
                      incorrect (labelled not owned), or unsupported (no label says owned).
                      Without labels this is "not_evaluated" -- a 0 there would be meaningless.
  useful_followups    follow-up jobs that acquired a 2xx capture yielding at least one observation.
  unnecessary_calls   successful (2xx) captures that yielded no observation, plus repeated
                      follow-ups of an already-followed URL/target.
  cost                Harvest's ENFORCED counters (requests/bytes/model) for the whole
                      investigation; external-tool network cost is an estimate, labelled so.

Usage:
    HARVEST_API_URL=... HARVEST_API_TOKEN=... python scripts/harness_eval.py <job_id> \\
        [--labels labels.json] [--answer answer.json]

`job_id` may be any job of the investigation; the root and every follow-up are scored together.
Prints one JSON object. Reads only; starts no scan.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from functools import cache

import httpx

_OWNERSHIP_ASSERTED = {"confirmed", "self"}


def _api() -> httpx.Client:
    base = os.environ["HARVEST_API_URL"].rstrip("/")
    token = os.environ["HARVEST_API_TOKEN"]
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=60.0)


def _all(c: httpx.Client, path: str, key: str = "id") -> list[dict]:
    out, after = [], 0
    while True:
        rows = c.get(path, params={"after": after, "limit": 500}).json()
        if not isinstance(rows, list):
            raise SystemExit(f"{path} did not return a list (got {rows}); check the job id")
        out.extend(rows)
        if len(rows) < 500:
            return out
        after = rows[-1][key]


def _pointer(doc, pointer: str):
    """Resolve an RFC 6901 JSON pointer; raise LookupError when it does not resolve."""
    for raw in pointer.lstrip("/").split("/") if pointer else []:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(doc, list):
            doc = doc[int(part)]
        elif isinstance(doc, dict):
            doc = doc[part]
        else:
            raise LookupError(pointer)
    return doc


def _unpack(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def support(obs: dict, body_of) -> str:
    """'derived' | 'supported' | 'unsupported' | 'uncited' for one observation."""
    if str(obs.get("method", "")).startswith("derived:"):
        return "derived"
    if not obs.get("capture_ids") or not obs.get("locator"):
        return "uncited"
    value = _unpack(obs["value"])
    for capture_id in obs["capture_ids"]:
        body = body_of(capture_id)
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if doc is not None and obs["locator"].startswith("/"):
            try:
                if _pointer(doc, obs["locator"]) == value:
                    return "supported"
            except (LookupError, ValueError, IndexError):
                pass
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            # Page extractions decode entities and type numbers, so compare against the
            # decoded text and the scalar's own spelling.
            needle = str(value)
            if needle and (needle in body or needle in html.unescape(body)):
                return "supported"
    return "unsupported"


def score_attribution(asserted: set[str], labels: dict | None) -> dict:
    if labels is None:
        return {"status": "not_evaluated", "reason": "no labelled case supplied"}
    owned = set(labels.get("owned", []))
    not_owned = set(labels.get("not_owned", []))
    return {
        "asserted": len(asserted),
        "correct": len(asserted & owned),
        "incorrect": sorted(asserted & not_owned),
        "unsupported": sorted(asserted - owned - not_owned),
        "missed_owned": sorted(owned - asserted),
        "errors": len(asserted - owned),
    }


def evaluate(c: httpx.Client, job_id: str, labels=None, answer=None) -> dict:
    inv = c.get(f"/jobs/{job_id}").json()["investigation"]
    jobs = [c.get(f"/jobs/{j}").json() for j in inv["job_ids"]]
    observations, captures, followups = [], [], []
    useful = 0
    for job in jobs:
        job_obs = _all(c, f"/jobs/{job['id']}/observations")
        job_caps = c.get(f"/jobs/{job['id']}/captures", params={"limit": 1000}).json()
        observations += job_obs
        captures += job_caps
        if job.get("parent_id") and job.get("root_id"):
            # A follow-up was useful if it acquired a successful capture that yielded evidence.
            useful += bool(job_obs) and any(200 <= cap["status"] < 300 for cap in job_caps)
            spec = job["spec"]
            followups.append(
                (spec.get("seeds") or [None])[0]
                or "tool:" + ":".join(f"{t['name']}/{t['target']}" for t in spec.get("tools", []))
            )

    @cache
    def body_of(capture_id: int) -> str:
        return c.get(f"/captures/{capture_id}/body").content.decode("utf-8", "replace")

    verdicts = [support(o, body_of) for o in observations]
    cited = sum(v != "uncited" for v in verdicts)
    checkable = sum(v in ("supported", "unsupported") for v in verdicts)
    supported = verdicts.count("supported")

    harvest_asserted = {
        o["entity_key"].removeprefix("url:")
        for o in observations
        if o["field"] == "ownership" and _unpack(o["value"]) in _OWNERSHIP_ASSERTED
    }
    obs_captures = {cid for o in observations for cid in o["capture_ids"]}
    empty_ok = sum(
        1 for cap in captures if cap["id"] not in obs_captures and 200 <= cap["status"] < 300
    )
    repeats = len(followups) - len(set(followups))
    return {
        "root_id": inv["root_id"],
        "jobs": len(jobs),
        "statuses": sorted({j["status"] for j in jobs}),
        "followups": len(followups),
        "useful_followups": useful,
        "followup_targets": followups,
        "observations": len(observations),
        "captures": len(captures),
        "citation_presence": round(cited / len(verdicts), 3) if verdicts else "not_evaluated",
        "evidence_support": round(supported / checkable, 3) if checkable else "not_evaluated",
        "evidence_checked": checkable,
        "derived_claims": verdicts.count("derived"),
        "attribution_harvest": score_attribution(harvest_asserted, labels),
        "attribution_answer": score_attribution(set(answer.get("owned", [])), labels)
        if answer is not None
        else {"status": "not_evaluated", "reason": "no harness answer supplied"},
        "unnecessary_calls": empty_ok + repeats,
        "repeated_followups": repeats,
        "cost_enforced": {k: v["used"] for k, v in inv["enforced"].items()},
        "budget_limits": {k: v["limit"] for k, v in inv["enforced"].items()},
        "external_tool_runs": inv["external_tool_runs"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_id")
    parser.add_argument("--labels", help="JSON {owned: [...], not_owned: [...]}")
    parser.add_argument("--answer", help="harness final answer JSON with an 'owned' list")
    args = parser.parse_args()
    labels = json.load(open(args.labels)) if args.labels else None
    answer = json.load(open(args.answer)) if args.answer else None
    with _api() as c:
        print(json.dumps(evaluate(c, args.job_id, labels, answer)))


if __name__ == "__main__":
    sys.exit(main())
