#!/usr/bin/env python3
"""Compare matched native/Harvest artifact pairs by finding identity and field retention.

No live acquisition is launched here. Live runners must supply two manifests with identical
settings and retain their invocation ledger (maximum twelve). A settings mismatch fails.
"""

import argparse
import json
from pathlib import Path

from harvest.tools import (
    _ghunt_records,
    _maigret_record,
    _spiderfoot_records,
    spiderfoot_exclusions,
)

MATCH = (
    "tool",
    "version",
    "target",
    "database_sha256",
    "modules",
    "credential_fingerprint",
    "proxy_fingerprint",
    "retries",
    "concurrency",
    "deadline_seconds",
    "top_sites",
)


def normalized(tool, native, target):
    if tool == "spiderfoot":
        events = native["events"]
        return _spiderfoot_records(events, target), spiderfoot_exclusions(events, target)
    if tool == "ghunt":
        return _ghunt_records(native, target), [
            {"container": k, "reason": "empty profile"}
            for k, v in native.items()
            if not v.get("profile")
        ]
    if tool == "maigret":
        records = [
            _maigret_record(k, v, target)
            for k, v in native.items()
            if isinstance(v, dict)
            and v.get("url_user")
            and str(v.get("status", {}).get("status", "")).lower() == "claimed"
        ]
        exclusions = [
            {"site": k, "reason": "no conclusive found profile"}
            for k, v in native.items()
            if not isinstance(v, dict)
            or not v.get("url_user")
            or str(v.get("status", {}).get("status", "")).lower() != "claimed"
        ]
        return records, exclusions
    raise ValueError("unsupported tool")


def compare(reference, candidate):
    mismatches = [
        k
        for k in MATCH
        if k not in reference["settings"]
        or reference["settings"].get(k) != candidate["settings"].get(k)
    ]
    if mismatches:
        raise ValueError("unmatched acquisition settings: " + ", ".join(mismatches))
    settings = reference["settings"]
    expected, exclusions = normalized(settings["tool"], reference["native"], settings["target"])
    observed = candidate["results"]

    def key(record):
        return (
            record.get("url") or record.get("id") or (record.get("event_type"), record.get("data"))
        )

    # A duplicate can be merged into a related finding without losing any of its fields.
    actual = {str(key(r)): r for r in observed}
    for record in observed:
        for related in record.get("related_findings", []):
            actual[str(key(related))] = related
            exclusions.append(
                {
                    "finding": str(key(related)),
                    "reason": "duplicate profile; full fields retained under related_findings",
                }
            )
    missing = []

    def retained_fields(value, got, finding, path=""):
        if isinstance(value, dict) and isinstance(got, dict):
            for field, item in value.items():
                retained_fields(item, got.get(field), finding, path + "/" + field)
        elif value != got:
            missing.append({"finding": finding, "field": path})

    for record in expected:
        match = actual.get(str(key(record)), {})
        retained_fields(record, match, str(key(record)))
    return {
        "valid_unique_findings": len({str(key(r)) for r in expected}),
        "exclusions": exclusions,
        "missing_or_changed_fields": missing,
        "passed": not missing,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.reference.read_text()), json.loads(args.candidate.read_text()))
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
