"""Verify a schema-1/2 database upgrade on a new backup, never on the original."""

import argparse
import json
import sqlite3
from pathlib import Path

from harvest.models import JobSpec
from harvest.store import Store, digest, packed


def snapshot(db):
    tables = (
        "jobs",
        "tasks",
        "events",
        "blobs",
        "captures",
        "entities",
        "observations",
        "sightings",
        "schedules",
    )
    counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables}
    # Immutable provenance, values and schedule state must be byte-for-byte unchanged.
    hashes = {}
    for table in ("captures", "observations", "sightings", "schedules"):
        rows = [list(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
        hashes[table] = digest(packed(rows))
    hashes["blobs"] = digest(
        packed([(r[0], digest(r[1])) for r in db.execute("SELECT * FROM blobs ORDER BY hash")])
    )
    return {"counts": counts, "hashes": hashes}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    source, destination = Path(args.source).resolve(), Path(args.destination).resolve()
    if destination.exists():
        parser.error("destination must not already exist")
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        source_version = original.execute("PRAGMA user_version").fetchone()[0]
        assert source_version in (1, 2)
        with sqlite3.connect(destination) as target:
            original.backup(target)
    with sqlite3.connect(destination) as db:
        before = snapshot(db)
    store = Store(destination)
    with store.connection() as db:
        after = snapshot(db)
        assert before == after
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        keys = db.execute(
            "SELECT id,spec,idempotency_key,execution FROM jobs WHERE idempotency_key IS NOT NULL"
        ).fetchall()
        revisions = db.execute("SELECT count(*) FROM extractions").fetchone()[0]
        memberships = db.execute("SELECT count(*) FROM assertions").fetchone()[0]
    for job in keys:
        if job["execution"] == "offline_replay":
            from harvest.models import ReplaySpec

            ids = [x["capture_id"] for x in store.extractions(job["id"])]
            assert (
                store.replay(
                    ReplaySpec(
                        capture_ids=ids, limits=JobSpec.model_validate_json(job["spec"]).limits
                    ),
                    job["idempotency_key"],
                )
                == job["id"]
            )
        else:
            assert (
                store.create(JobSpec.model_validate_json(job["spec"]), [], job["idempotency_key"])
                == job["id"]
            )
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        assert original.execute("PRAGMA user_version").fetchone()[0] == source_version
    print(
        json.dumps(
            {
                "source_schema": source_version,
                "destination_schema": 3,
                "original_unchanged": True,
                "preserved": before,
                "extractions": revisions,
                "assertions": memberships,
                "idempotency_keys_verified": len(keys),
                "integrity_check": "ok",
                "foreign_key_check": [],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
