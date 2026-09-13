import json
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from harvest.engine import Engine
from harvest.extract import JsonAdapter
from harvest.models import JobSpec, Limits, LostLease, ReplaySpec


def submit(engine, source, rows=250, media="json", **limits):
    records = [{"id": i, "name": f"Organization {i}", "employees": i + 2} for i in range(rows)]
    if media == "json":
        body, content_type = {"items": records}, "application/json"
    else:
        body = "id,name,employees\n" + "\n".join(
            f"{r['id']},{r['name']},{r['employees']}" for r in records
        )
        content_type = "text/csv"
    path = "/population." + media
    source["routes"] = {path: (body, content_type)}
    spec = JobSpec(
        objective="Enumerate the organization register",
        mode="enumerative",
        seeds=[source["base"] + path],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0, **limits),
    )
    return engine.submit(spec), spec


@pytest.mark.parametrize("media", ["json", "csv"])
def test_entire_register_with_progress_and_exact_provenance(engine, source, media):
    job, _ = submit(engine, source, media=media)
    result = engine.run(job)
    rows = engine.store.observations(job, limit=10000)
    assert result["status"] == "completed"
    assert len({r["entity_key"] for r in rows}) == 250
    assert result["records_processed"] == 250 and result["claims_processed"] == 750
    extraction = engine.store.extractions(job)[0]
    assert (
        extraction["records_processed"],
        extraction["records_total"],
        extraction["records_remaining"],
        extraction["batches"],
    ) == (250, 250, 0, 5)
    last = next(r for r in rows if r["field"] == "name" and r["value"] == "Organization 249")
    assert last["locator"] == ("/items/249/name" if media == "json" else "row/249/name")
    assert last["capture_ids"] and last["extraction_ids"] and last["evidence"]
    assert source["counts"]["/population." + media] == 1


def interrupt_after_first_batch(engine, monkeypatch, exception):
    finish = engine.store.finish

    def interrupted(task, **kwargs):
        result = finish(task, **kwargs)
        if kwargs.get("batch") and not kwargs["batch"].done:
            raise exception
        return result

    monkeypatch.setattr(engine.store, "finish", interrupted)


@pytest.mark.parametrize("media", ["json", "csv"])
def test_restart_resumes_cursor_without_duplicate_claims_or_get(engine, source, monkeypatch, media):
    job, _ = submit(engine, source, media=media)
    engine.step(job)
    task = engine.store.claim(job, lease_seconds=0.1)
    interrupt_after_first_batch(engine, monkeypatch, SystemExit("crash after batch commit"))
    with pytest.raises(SystemExit):
        engine.process(task)
    assert engine.store.extractions(job)[0]["records_processed"] == 50
    assert engine.store.job(job)["claims_processed"] == 150
    assert engine.store.extractions(job)[0]["outcome"] is None
    # Partial results are available in this job, but have not replaced a complete current view.
    assert len(engine.store.observations(job, limit=1000)) == 150
    assert all(not e["fields"] for e in engine.store.canonical("default"))
    with engine.store.transaction() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    restarted = Engine(engine.settings)
    assert restarted.run(job)["status"] == "completed"
    result = restarted.store.job(job)
    assert result["records_processed"] == 250 and result["claims_processed"] == 750
    assert restarted.store.extractions(job)[0]["batches"] == 5
    assert source["counts"]["/population." + media] == 1


def test_batch_write_failure_rolls_back_cursor_counters_and_claims(engine, source, monkeypatch):
    job, _ = submit(engine, source)
    engine.step(job)
    task = engine.store.claim(job)
    finish = engine.store.finish

    def inject_after_checkpoint(task, **kwargs):
        result = finish(task, **kwargs)
        if kwargs.get("batch") and kwargs["batch"].end == 50:
            with engine.store.connection() as db:
                db.execute(
                    "CREATE TRIGGER fail_observation BEFORE INSERT ON observations BEGIN SELECT RAISE(ABORT,'batch fault'); END"
                )
        return result

    monkeypatch.setattr(engine.store, "finish", inject_after_checkpoint)
    with pytest.raises(sqlite3.IntegrityError, match="batch fault"):
        engine.process(task)
    assert engine.store.job(job)["records_processed"] == 50
    assert engine.store.job(job)["claims_processed"] == 150
    assert engine.store.extractions(job)[0]["records_processed"] == 50
    assert len(engine.store.observations(job, limit=10000)) == 150
    with engine.store.transaction() as db:
        db.execute("DROP TRIGGER fail_observation")
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    assert Engine(engine.settings).run(job)["status"] == "completed"


@pytest.mark.parametrize("media,total", [("json", 250), ("csv", None)])
def test_record_budget_preserves_prefix_with_honest_remaining(engine, source, media, total):
    job, _ = submit(engine, source, media=media, records=125)
    result = engine.run(job)
    assert result["status"] == "budget_exhausted"
    assert result["records_processed"] == 125 and result["claims_processed"] == 375
    extraction = engine.store.extractions(job)[0]
    assert extraction["records_total"] == total
    assert extraction["records_remaining"] == (125 if total else None)
    assert extraction["outcome"] is None
    # Increasing the budget requires a new replay; the terminal job remains immutable.
    replay = engine.replay(ReplaySpec(capture_ids=[1]))
    assert engine.run(replay)["status"] == "completed"
    assert len(engine.store.observations(replay, limit=10000)) == 750
    assert engine.store.job(job)["records_processed"] == 125


def test_claim_budget_is_global_and_atomic_per_batch(engine, source):
    job, _ = submit(engine, source, claims=170)
    result = engine.run(job)
    assert result["status"] == "budget_exhausted"
    assert result["claims_processed"] == 150 and result["records_processed"] == 50
    assert len(engine.store.observations(job, limit=1000)) == 150


def test_wide_records_no_longer_hit_response_claim_cap(engine, source):
    job, _ = submit(engine, source, rows=51)
    source["routes"]["/population.json"] = (
        [{"id": i, **{f"field{n}": n for n in range(99)}} for i in range(51)],
        "application/json",
    )
    result = engine.run(job)
    assert result["status"] == "completed"
    assert result["claims_processed"] == 5100
    assert len(engine.store.observations(job, limit=6000)) == 5100


@pytest.mark.parametrize(
    "body", ["id,name\n1,Alpha\n2\n", "id,id\n1,2\n", 'id,name\n1,"unterminated']
)
def test_bad_csv_is_retained_but_never_reported_complete(engine, source, body):
    job, _ = submit(engine, source, media="csv")
    source["routes"]["/population.csv"] = (body, "text/csv")
    result = engine.run(job)
    assert result["status"] == "partial"
    assert engine.store.extractions(job)[0]["status"] == "failed"
    assert engine.store.capture(1)["body"] == body.encode()


def test_csv_multiline_quotes_unicode_and_global_row_locator(engine, source):
    job, _ = submit(engine, source, media="csv", rows=52)
    body = "id,name\r\n" + "".join(
        f'{i},"Company {i}, München\r\nSecond line"\r\n' for i in range(52)
    )
    source["routes"]["/population.csv"] = (body, "text/csv")
    assert engine.run(job)["status"] == "completed"
    last = next(
        o for o in engine.store.observations(job, limit=1000) if o["locator"] == "row/51/name"
    )
    assert last["value"] == "Company 51, München\r\nSecond line"


def test_stale_token_and_cancel_after_batch_cannot_advance_cursor(engine, source, monkeypatch):
    job, _ = submit(engine, source)
    engine.step(job)
    task = engine.store.claim(job)
    interrupt_after_first_batch(engine, monkeypatch, SystemExit())
    with pytest.raises(SystemExit):
        engine.process(task)
    engine.store.stop(job)
    with pytest.raises(LostLease):
        engine.process(task)
    assert engine.store.extractions(job)[0]["records_processed"] == 50
    assert engine.store.job(job)["claims_processed"] == 150


def test_zero_records_completes_and_legacy_adapter_limits_remain_explicit(engine, source):
    job, _ = submit(engine, source, rows=0)
    assert engine.run(job)["status"] == "completed"
    assert engine.store.extractions(job)[0]["records_total"] == 0
    assert engine.store.job(job)["records_processed"] == 0


def test_warnings_from_early_batch_survive_to_final_outcome(engine, source):
    job, _ = submit(engine, source, rows=101)
    records = [{"id": i, "name": str(i)} for i in range(101)]
    records[0]["too_long"] = "x" * 21000
    source["routes"]["/population.json"] = (records, "application/json")
    assert engine.run(job)["status"] == "partial"
    assert engine.store.extractions(job)[0]["records_processed"] == 101
    assert engine.store.extractions(job)[0]["had_warnings"] == 1


def test_parser_is_not_restarted_for_each_batch(engine, source, monkeypatch):
    job, _ = submit(engine, source, rows=250)
    original = JsonAdapter.records
    calls = []

    def records(self, body):
        calls.append(len(body))
        return original(self, body)

    monkeypatch.setattr(JsonAdapter, "records", records)
    assert engine.run(job)["status"] == "completed"
    assert len(calls) == 1


def test_incomplete_refresh_does_not_publish_a_partial_replacement(engine, source):
    job, spec = submit(engine, source, rows=101)
    assert engine.run(job)["status"] == "completed"
    spec.limits.records = 50
    source["routes"]["/population.json"] = (
        {"items": [{"id": i, "name": "Updated", "employees": 1} for i in range(101)]},
        "application/json",
    )
    second = engine.run(engine.submit(spec))
    assert second["status"] == "budget_exhausted"
    current = engine.store.canonical("default", limit=1000)
    assert len(current) == 101
    assert all(e["fields"]["name"]["value"].startswith("Organization") for e in current)
    assert all(e["source_states"][0]["stale"] for e in current)
    assert any(o["value"] == "Updated" for o in engine.store.observations(second["id"], limit=1000))


def test_record_budget_shared_across_pagination(engine, source):
    job, _ = submit(engine, source, rows=75, records=100)
    original, media = source["routes"]["/population.json"]
    original["next"] = source["base"] + "/more.json"
    source["routes"]["/more.json"] = (
        {"items": [{"id": i + 75, "name": str(i)} for i in range(75)]},
        media,
    )
    result = engine.run(job)
    assert result["status"] == "budget_exhausted"
    assert result["records_processed"] == 100
    assert [x["records_processed"] for x in engine.store.extractions(job)] == [75, 25]
    assert source["counts"]["/more.json"] == 1


def test_model_claims_share_global_claim_limit(engine, source):
    settings = engine.settings
    settings.model_url = source["base"] + "/v1"
    settings.model_name = "fixture-model"
    settings.model_usd_per_million = 0
    engine = Engine(settings)
    spec = JobSpec(
        objective="Find protocol",
        seeds=[source["base"] + "/page"],
        fields=["protocol"],
        use_model=True,
        limits=Limits(depth=0, domain_delay=0.1, claims=1),
    )
    result = engine.run(engine.submit(spec))
    assert result["status"] == "budget_exhausted"
    assert result["claims_processed"] == 1
    assert engine.store.observations(result["id"])[0]["field"] == "page_title"


def test_late_csv_error_preserves_committed_rows_without_publishing_revision(engine, source):
    job, _ = submit(engine, source, media="csv")
    body = "id,name\n" + "\n".join(f"{i},Name{i}" for i in range(115)) + '\n116,"unterminated'
    source["routes"]["/population.csv"] = (body, "text/csv")
    result = engine.run(job)
    assert result["status"] == "partial"
    assert engine.store.extractions(job)[0]["records_processed"] == 100
    assert engine.store.extractions(job)[0]["records_total"] is None
    assert engine.store.extractions(job)[0]["outcome"] is None
    assert len(engine.store.observations(job, limit=1000)) == 200


def test_replay_key_survives_addition_of_default_limits(engine, source):
    job, _ = submit(engine, source, rows=2)
    engine.run(job)
    replay = engine.replay(ReplaySpec(capture_ids=[1]), "legacy-replay")
    engine.run(replay)
    from harvest.store import digest, packed

    with engine.store.transaction() as db:
        spec = json.loads(db.execute("SELECT spec FROM jobs WHERE id=?", (replay,)).fetchone()[0])
        del spec["limits"]["records"]
        del spec["limits"]["claims"]
        db.execute(
            "UPDATE jobs SET spec=?,spec_hash=? WHERE id=?",
            (packed(spec), digest(packed({"spec": spec, "capture_ids": [1]})), replay),
        )
    assert engine.replay(ReplaySpec(capture_ids=[1]), "legacy-replay") == replay


def test_changed_body_cannot_resume_at_saved_cursor(engine, source, monkeypatch):
    job, _ = submit(engine, source)
    engine.step(job)
    task = engine.store.claim(job)
    interrupt_after_first_batch(engine, monkeypatch, SystemExit())
    with pytest.raises(SystemExit):
        engine.process(task)
    with engine.store.transaction() as db:
        db.execute("UPDATE blobs SET body=?", (b"[]",))
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    result = Engine(engine.settings).run(job)
    assert result["status"] == "partial"
    assert engine.store.extractions(job)[0]["records_processed"] == 50


def test_schema_two_batch_migration_is_atomic_and_unknown_counts_stay_unknown(tmp_path):
    from test_replay import make_schema_one

    from harvest.store import Store

    path = tmp_path / "old.sqlite"
    make_schema_one(path)

    class SchemaTwo(Store):
        def _migrate_batches(self):
            pass

    SchemaTwo(path)

    class FailedUpgrade(Store):
        def _migrate(self):
            pass

        @contextmanager
        def transaction(self):
            with super().transaction() as db:
                yield db
                raise RuntimeError("batch migration fault")

    with pytest.raises(RuntimeError, match="batch migration fault"):
        FailedUpgrade(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "records_processed" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    store = Store(path)
    assert store.extractions("old")[0]["records_processed"] is None
    assert store.extractions("old")[0]["records_total"] is None
    assert store.job("old")["claims_processed"] == 1
    assert store.observations("old")[0]["value"] == "Legacy"


def test_process_death_after_batch_commit_resumes_without_reacquisition(engine, source):
    job, _ = submit(engine, source)
    engine.step(job)
    code = """
import os,sys
from harvest.config import Settings
from harvest.engine import Engine
engine=Engine(Settings(database=sys.argv[1],proxy=None))
finish=engine.store.finish
def checkpoint(task, **kwargs):
 result=finish(task, **kwargs)
 if kwargs.get('batch') and kwargs['batch'].end==50:
  os._exit(27)
 return result
engine.store.finish=checkpoint
task=engine.store.claim(sys.argv[2],lease_seconds=0.2)
engine.process(task)
"""
    result = subprocess.run([sys.executable, "-c", code, engine.store.path, job], timeout=10)
    assert result.returncode == 27
    assert engine.store.extractions(job)[0]["records_processed"] == 50
    time.sleep(0.21)
    restarted = Engine(engine.settings)
    assert restarted.run(job)["status"] == "completed"
    assert restarted.store.job(job)["claims_processed"] == 750
    assert restarted.store.extractions(job)[0]["batches"] == 5
    assert source["counts"]["/population.json"] == 1
