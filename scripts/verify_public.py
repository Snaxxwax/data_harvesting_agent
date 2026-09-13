"""Opt-in, public GitHub metadata + refresh + offline replay acceptance run.

Uses normal HARVEST_* configuration. No credentials or model endpoint are required.
The output is a report on stdout; use a new --db path to retain inspected evidence.
"""

import argparse
import json

from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import JobSpec, Limits, ReplaySpec


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    settings = Settings(database=args.db)
    engine = Engine(settings)
    spec = JobSpec(
        objective="Inspect HTTPX official repository metadata",
        dataset="httpx-public",
        seeds=["https://api.github.com/repos/encode/httpx"],
        allowed_domains=["api.github.com"],
        fields=["full_name", "description"],
        limits=Limits(depth=0, requests=10, seconds=120),
    )
    first = engine.run(engine.submit(spec, "public-initial"))
    assert first["status"] == "completed", first
    capture = engine.store.captures(first["id"])[0]
    observations = engine.store.observations(first["id"], limit=1000)
    assert any(o["field"] == "full_name" and o["value"] == "encode/httpx" for o in observations)
    same = engine.run(engine.submit(spec, "public-initial"))
    assert same["requests"] == first["requests"]
    refreshed = engine.run(engine.submit(spec, "public-refresh"))
    assert refreshed["status"] == "completed", refreshed
    replay = engine.run(engine.replay(ReplaySpec(capture_ids=[capture["id"]]), "public-replay"))
    assert replay["status"] == "completed" and replay["requests"] == 0
    assert engine.store.capture(capture["id"])["retrieved"] == capture["retrieved"]
    assert {o["id"] for o in engine.store.observations(replay["id"], limit=1000)} == {
        o["id"] for o in observations
    }
    with engine.store.connection() as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
    report = {"source": spec.seeds[0], "integrity_check": integrity, "runs": []}
    for label, result in (("initial", first), ("refresh", refreshed), ("offline_replay", replay)):
        report["runs"].append(
            {
                "label": label,
                **{
                    k: result[k]
                    for k in (
                        "id",
                        "status",
                        "captures",
                        "evidence_captures",
                        "requests",
                        "bytes",
                        "model_calls",
                        "extraction_progress",
                    )
                },
                "observations": len(engine.store.observations(result["id"], limit=1000)),
                "evidence": [
                    {
                        k: c[k]
                        for k in (
                            "id",
                            "job_id",
                            "url",
                            "retrieved",
                            "status",
                            "body_hash",
                            "previous_id",
                            "changed",
                        )
                    }
                    for c in engine.store.captures(result["id"])
                ],
            }
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
