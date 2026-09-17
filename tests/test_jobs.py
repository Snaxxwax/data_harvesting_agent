import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from harvest.engine import Engine
from harvest.models import JobSpec, Limits, LostLease


def spec(source, path="/records", **kwargs):
    return JobSpec(
        objective="Collect project records",
        seeds=[source["base"] + path],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=2, seconds=30),
        **kwargs,
    )


def table_count(store, table):
    with store.connection() as db:
        return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_real_http_pagination_provenance_idempotency_and_refresh(engine, source):
    job_spec = spec(source, mode="enumerative")
    job = engine.submit(job_spec, "first")
    result = engine.run(job)
    assert result["status"] == "completed"
    assert result["captures"] == 2
    observations = engine.store.observations(job)
    assert {r["value"] for r in observations if r["field"] == "name"} == {"Alpha", "Beta"}
    assert all(r["evidence"] and r["locator"] and r["capture_ids"] for r in observations)
    assert len(engine.store.canonical("default")) == 2
    before = table_count(engine.store, "observations")
    count = len(source["requests"])
    assert engine.submit(job_spec, "first") == job
    engine.run(job)
    assert len(source["requests"]) == count
    again = engine.submit(job_spec, "refresh")
    engine.run(again)
    assert table_count(engine.store, "observations") == before
    with engine.store.connection() as db:
        cap = db.execute(
            "SELECT * FROM captures WHERE job_id=? AND url LIKE '%/records'", (again,)
        ).fetchone()
        assert cap["status"] == 304 and cap["changed"] == 0 and cap["previous_id"]
    source["version"] = 2
    refreshed = engine.submit(job_spec)
    engine.run(refreshed)
    assert table_count(engine.store, "observations") == before + 1
    alpha = next(x for x in engine.store.canonical("default") if "alpha" in x["entity_key"])
    assert alpha["fields"]["size"]["value"] == 2
    assert alpha["fields"]["size"]["conflict"] is False


def test_conflicting_sources_preserved(engine, source):
    job_spec = spec(source)
    job_spec.seeds.append(source["base"] + "/conflict")
    job = engine.submit(job_spec)
    assert engine.run(job)["status"] == "completed"
    alpha = next(x for x in engine.store.canonical("default") if "alpha" in x["entity_key"])
    name = alpha["fields"]["name"]
    assert name["conflict"] and name["value"] is None
    assert {x["value"] for x in name["candidates"]} == {"Alpha", "Different Alpha"}


def test_idempotency_key_conflict(engine, source):
    engine.submit(spec(source), "key")
    with pytest.raises(ValueError, match="different job"):
        engine.submit(spec(source, "/page2"), "key")


def test_late_committed_older_capture_does_not_replace_newer_evidence(engine, source):
    first = engine.submit(spec(source))
    engine.run(first)
    source["version"] = 2
    second = engine.submit(spec(source))
    engine.run(second)
    # Simulate an older retrieval finishing extraction after a newer retrieval committed.
    with engine.store.transaction() as db:
        db.execute("UPDATE captures SET retrieved=0 WHERE job_id=?", (second,))
    alpha = next(x for x in engine.store.canonical("default") if "alpha" in x["entity_key"])
    assert alpha["fields"]["size"]["value"] == 1


def test_request_budget_includes_robots(engine, source):
    job_spec = spec(source)
    job_spec.limits.requests = 1
    job = engine.submit(job_spec)
    result = engine.run(job)
    assert result["status"] == "budget_exhausted"
    assert result["requests"] == 1
    assert source["requests"] == ["/robots.txt"]
    assert table_count(engine.store, "captures") == 0


def test_robots_checked_after_redirect(engine, source):
    job = engine.submit(spec(source, "/redirect"))
    result = engine.run(job)
    assert result["status"] == "failed"
    assert "/private" not in source["requests"]
    assert result["progress"]["blocked"] == 1


def test_partial_failure_does_not_discard_success(engine, source):
    job_spec = spec(source, "/page2")
    job_spec.seeds.append(source["base"] + "/bad")
    result = engine.run(engine.submit(job_spec))
    assert result["status"] == "partial"
    assert result["captures"] == 2  # Malformed evidence survives; only extraction fails.
    assert result["extraction_progress"] == {"complete": 1, "failed": 1}


def test_retry_recovers_503(engine, source):
    result = engine.run(engine.submit(spec(source, "/unstable")))
    assert result["status"] == "completed"
    assert source["counts"]["/unstable"] == 2
    assert any(x["type"] == "deferred" for x in engine.store.events(result["id"]))


def test_response_size_bound(engine, source):
    job_spec = spec(source, "/large")
    job_spec.limits.response_bytes = 1024
    result = engine.run(engine.submit(job_spec))
    assert result["status"] == "failed"
    assert result["captures"] == 0
    assert table_count(engine.store, "blobs") == 0


def test_stale_worker_cannot_commit_after_reclaim(engine, source):
    job = engine.submit(spec(source, "/page2"))
    first = engine.store.claim(job, lease_seconds=0.01)
    time.sleep(0.02)
    second = engine.store.claim(job)
    assert second["token"] != first["token"]
    with pytest.raises(LostLease):
        engine.store.finish(first)
    engine.process(second)
    engine.run(job)  # Acquisition now checkpoints a separate extraction task.
    engine.store.settle(job)
    assert engine.store.job(job)["status"] == "completed"


def test_cancellation_invalidates_worker_and_budget(engine, source):
    job = engine.submit(spec(source))
    task = engine.store.claim(job)
    engine.store.stop(job)
    with pytest.raises(LostLease):
        engine.store.reserve(task, requests=1)
    with pytest.raises(LostLease):
        engine.store.finish(task)
    assert engine.store.job(job)["status"] == "cancelled"


def test_two_workers_never_claim_same_job_concurrently(engine, source):
    job = engine.submit(spec(source))
    with ThreadPoolExecutor(2) as pool:
        tasks = list(pool.map(lambda _: engine.store.claim(job), range(2)))
    assert sum(t is not None for t in tasks) == 1


def test_atomic_extraction_failure_preserves_checkpoint_and_rolls_back_results(engine, source):
    job = engine.submit(spec(source, "/page2"))
    task = engine.store.claim(job)
    engine.process(task)
    task = engine.store.claim(job)
    assert task["kind"] == "extract"
    requests = len(source["requests"])
    with engine.store.connection() as db:
        db.execute(
            "CREATE TRIGGER simulate_disk_failure BEFORE INSERT ON observations BEGIN SELECT RAISE(ABORT,'fault injection'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fault injection"):
        engine.process(task)
    assert table_count(engine.store, "captures") == 1
    assert table_count(engine.store, "blobs") == 1
    assert table_count(engine.store, "observations") == 0
    assert table_count(engine.store, "assertions") == 0
    assert engine.store.extractions(job)[0]["outcome"] is None
    with engine.store.transaction() as db:
        db.execute("DROP TRIGGER simulate_disk_failure")
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    restarted = Engine(engine.settings)
    assert restarted.run(job)["status"] == "completed"
    assert len(source["requests"]) == requests


def test_continuous_schedule_is_durable_and_does_not_overlap(engine, source):
    job = engine.submit(spec(source, "/page2", mode="continuous", refresh_seconds=60))
    with engine.store.transaction() as db:
        db.execute("UPDATE schedules SET next_run=0")
    engine.store.schedule_tick()
    assert table_count(engine.store, "jobs") == 1
    engine.run(job)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: engine.store.schedule_tick(), range(2)))
    assert table_count(engine.store, "jobs") == 2
    child = next(x for x in engine.store.jobs() if x["id"] != job)
    assert engine.run(child["id"])["status"] == "completed"
    engine.store.disable_schedule(job)
    with engine.store.transaction() as db:
        db.execute("UPDATE schedules SET next_run=0")
    engine.store.schedule_tick()
    assert table_count(engine.store, "jobs") == 2


def test_online_backup_restores_provenance(engine, source, tmp_path):
    job = engine.submit(spec(source, "/page2"))
    engine.run(job)
    path = tmp_path / "backup.sqlite"
    engine.store.backup(path)
    from harvest.store import Store

    restored = Store(path)
    assert restored.job(job)["status"] == "completed"
    assert restored.observations(job) == engine.store.observations(job)
    assert restored.capture(1)["body"] == engine.store.capture(1)["body"]


def test_failure_after_last_attempt_lease_expiry_settles(engine, source):
    job_spec = spec(source)
    job_spec.limits.attempts = 1
    job = engine.submit(job_spec)
    engine.store.claim(job, lease_seconds=0.001)
    time.sleep(0.01)
    assert engine.run(job)["status"] == "failed"


def test_job_records_reflects_entity_fields_and_conflicts(engine, source):
    job_spec = spec(source)
    job_spec.seeds.append(source["base"] + "/conflict")
    job = engine.submit(job_spec)
    engine.run(job)
    records = engine.store.job_records(job)
    alpha = next(r for r in records if "alpha" in r["entity_key"])
    assert alpha["fields"]["name"]["conflict"] is True
    assert alpha["fields"]["name"]["value"] is None
    assert {c["value"] for c in alpha["fields"]["name"]["candidates"]} == {
        "Alpha",
        "Different Alpha",
    }
    assert all(
        c["source_url"] and c["evidence"] and c["locator"]
        for c in alpha["fields"]["name"]["candidates"]
    )


def test_rerun_creates_new_job_preserving_original_spec(engine, source):
    job_spec = spec(source, "/page2")
    original = engine.submit(job_spec)
    engine.run(original)
    rerun_id = engine.rerun(original)
    assert rerun_id != original
    assert engine.store.job(rerun_id)["spec"] == engine.store.job(original)["spec"]
    result = engine.run(rerun_id)
    assert result["status"] == "completed"
    # Original job's own history is untouched by the new run.
    assert engine.store.job(original)["status"] == "completed"


def test_rerun_rejects_continuous_jobs_to_avoid_duplicate_schedule(engine, source):
    job = engine.submit(spec(source, "/page2", mode="continuous", refresh_seconds=60))
    with pytest.raises(ValueError, match="schedule"):
        engine.rerun(job)


def test_job_records_backfills_requested_field_absent_everywhere(engine, source):
    job_spec = spec(source, fields=["name", "phantom_field"])
    job = engine.submit(job_spec)
    engine.run(job)
    records = engine.store.job_records(job)
    assert records
    for record in records:
        assert record["fields"]["phantom_field"] == {
            "value": None,
            "conflict": False,
            "missing": True,
            "candidates": [],
        }
        assert record["fields"]["name"]["missing"] is False
        assert record["fields"]["name"]["candidates"]
