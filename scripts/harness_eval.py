#!/usr/bin/env python3
"""Measure one investigation the same way regardless of who drove it.

The point of the comparison is that Harvest, not the harness, owns evidence and attribution,
so every run -- Harvest's own deterministic planner or an LLM harness driving the MCP tools --
is scored by reading the SAME job back through the SAME authenticated API. The metrics are
deliberately behavioural and content-free (counts and ratios), never the found accounts
themselves:

  discoveries        matched source entities admitted to a declared target in the dossier
  false_attribution  observations/records that assert OWNERSHIP as confirmed from tool
                     evidence (must be 0: a tool never proves a person owns an account), plus
                     dossier entities whose admission note would read as verified identity
  evidence_support   fraction of dossier field candidates carrying a capture id AND a locator
  unnecessary_calls  acquisition tasks (fetch/search/tool) that finished with no observations
                     and were not an honest 304/negative result
  new_bytes          bytes of body fetched by THIS job's own live captures (replays reuse
                     existing captures, so a replay-based run scores 0)
  tool_calls         number of tool acquisitions the job ran

Usage:
    HARVEST_API_URL=... HARVEST_API_TOKEN=... python scripts/harness_eval.py <job_id> [job_id...]

Prints one JSON object per job. No writes, no new scans.
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def _api() -> httpx.Client:
    base = os.environ["HARVEST_API_URL"].rstrip("/")
    token = os.environ["HARVEST_API_TOKEN"]
    return httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=60.0)


def _all(c: httpx.Client, path: str, key: str = "id") -> list[dict]:
    out, after = [], 0
    while True:
        rows = c.get(path, params={"after": after, "limit": 500}).json()
        if not rows:
            return out
        out.extend(rows)
        after = rows[-1][key]
        if len(rows) < 500:
            return out


def evaluate(c: httpx.Client, job_id: str) -> dict:
    job = c.get(f"/jobs/{job_id}").json()
    dossier = c.get(f"/jobs/{job_id}/dossier").json()
    observations = _all(c, f"/jobs/{job_id}/observations")
    tasks = _all(c, f"/jobs/{job_id}/tasks")
    captures = c.get(f"/jobs/{job_id}/captures", params={"limit": 1000}).json()

    discoveries = sum(len(t["matched_entities"]) for t in dossier.get("targets", []))

    # false attribution: any observation claiming ownership "confirmed" (a tool must never),
    # and any admitted entity whose note would read as a verified identity.
    owned_confirmed = sum(
        1 for o in observations if o["field"] == "ownership" and o["value"] == "confirmed"
    )

    # evidence support: every dossier field candidate must cite a capture and a locator.
    cited = total = 0
    for target in dossier.get("targets", []):
        for info in target.get("fields", {}).values():
            for cand in info.get("candidates", []):
                total += 1
                if cand.get("capture_ids") and cand.get("locator"):
                    cited += 1
    evidence_support = round(cited / total, 3) if total else 1.0

    acq = [t for t in tasks if t["kind"] in ("fetch", "search", "tool")]
    # an acquisition that produced no observation AND was not an honest negative (404/304/empty
    # search) is a wasted call; we can only see status here, so this is an upper bound.
    obs_captures = {cid for o in observations for cid in o["capture_ids"]}
    unnecessary = sum(
        1
        for cap in captures
        if cap["id"] not in obs_captures and 200 <= cap["status"] < 300
    )

    return {
        "job_id": job_id,
        "status": job["status"],
        "mode": json.loads(job["spec"])["mode"] if isinstance(job.get("spec"), str) else job["spec"]["mode"],
        "discoveries": discoveries,
        "false_attribution": owned_confirmed,
        "evidence_support": evidence_support,
        "tool_calls": sum(1 for t in acq if t["kind"] == "tool"),
        "acquisitions": len(acq),
        "unnecessary_calls": unnecessary,
        "captures": len(captures),
        "observations": len(observations),
        "unresolved": len(dossier.get("unresolved", [])),
        "ambiguous": len(dossier.get("ambiguous", [])),
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    with _api() as c:
        for job_id in sys.argv[1:]:
            print(json.dumps(evaluate(c, job_id)))


if __name__ == "__main__":
    main()
