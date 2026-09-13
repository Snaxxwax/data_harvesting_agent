import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from harvest.api import create_app
from harvest.engine import Engine
from harvest.extract import JsonAdapter
from harvest.models import Claim, Extraction, JobSpec, Lead, Limits, LostLease, ReplaySpec
from harvest.store import SCHEMA, Store, digest, packed


def job_spec(source, path="/page2", **kwargs):
    return JobSpec(
        objective="Collect primary records",
        seeds=[source["base"] + path],
        allowed_domains=["127.0.0.1"],
        limits=Limits(depth=0, domain_delay=0.1),
        **kwargs,
    )


def offline_engine(engine):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay constructed a network client")

    result = Engine(engine.settings, fetcher_factory=forbidden)
    result.reasoner.decide = forbidden
    return result


def test_replay_is_offline_idempotent_and_does_not_fabricate_capture(engine, source):
    original = engine.run(engine.submit(job_spec(source, "/records")))
    cap = engine.store.captures(original["id"])[0]
    prior = engine.store.observations(original["id"], limit=1000)
    count = len(source["requests"])
    worker = offline_engine(engine)
    request = ReplaySpec(capture_ids=[cap["id"]])
    replay = worker.replay(request, "replay-key")
    result = worker.run(replay)
    assert result["status"] == "completed"
    assert result["execution"] == "offline_replay"
    assert (
        result["captures"],
        result["evidence_captures"],
        result["requests"],
        result["bytes"],
        result["model_calls"],
    ) == (0, 1, 0, 0, 0)
    assert worker.store.capture(cap["id"]) == engine.store.capture(cap["id"])
    assert len(source["requests"]) == count
    rows = worker.store.observations(replay)
    assert all(r["capture_ids"] == [cap["id"]] and r["last_seen"] == cap["retrieved"] for r in rows)
    assert worker.store.observations(original["id"], limit=1000) == prior
    assert worker.replay(request, "replay-key") == replay
    worker.run(replay)
    assert len(worker.store.extractions(replay)) == 1
    other = worker.store.captures(original["id"])[1]["id"]
    with pytest.raises(ValueError, match="idempotency"):
        worker.replay(ReplaySpec(capture_ids=[other]), "replay-key")
    events = worker.store.events(replay)
    assert any(e["details"].get("suppressed_leads", 0) > 0 for e in events)


def test_failed_capture_recovered_with_installed_adapter_without_reacquisition(engine, source):
    original = engine.run(engine.submit(job_spec(source, "/bad")))
    cap = engine.store.captures(original["id"])[0]
    assert cap["body_hash"] == digest(b"{bad json")
    assert engine.store.extractions(original["id"])[0]["status"] == "failed"

    class DiagnosticAdapter:
        name = "fixture-diagnostic/1"

        def accepts(self, content_type, url):
            return url.endswith("/bad")

        def extract(self, body, url):
            return Extraction(
                extractor=self.name,
                claims=[
                    Claim(
                        entity_key="url:" + url,
                        field="source_error",
                        value="malformed JSON",
                        evidence=body.decode(),
                        locator="bytes:0",
                        method="diagnostic",
                    )
                ],
                leads=[Lead(url=source["base"] + "/private")],
            )

    worker = offline_engine(engine)
    worker.extractors.adapters.insert(0, DiagnosticAdapter())
    before = len(source["requests"])
    replay = worker.run(worker.replay(ReplaySpec(capture_ids=[cap["id"]])))
    assert replay["status"] == "completed"
    assert len(source["requests"]) == before
    assert worker.store.observations(replay["id"])[0]["value"] == "malformed JSON"
    assert worker.store.observations(original["id"]) == []
    assert worker.store.extractions(original["id"])[0]["status"] == "failed"


def test_revised_interpretation_is_not_a_false_source_conflict(engine, source):
    original = engine.run(engine.submit(job_spec(source)))
    cap = engine.store.captures(original["id"])[0]
    prior = engine.store.observations(original["id"])

    class UppercaseAdapter(JsonAdapter):
        name = "fixture-normalized-name/2"

        def extract(self, body, url):
            result = super().extract(body, url)
            for claim in result.claims:
                if claim.field == "name":
                    claim.value = claim.value.upper()
            return result

    worker = offline_engine(engine)
    worker.extractors.adapters.insert(0, UppercaseAdapter())
    replay = worker.run(worker.replay(ReplaySpec(capture_ids=[cap["id"]])))
    name = worker.store.canonical("default")[0]["fields"]["name"]
    assert name["value"] == "BETA" and not name["conflict"]
    assert worker.store.observations(original["id"]) == prior
    assert any(o["value"] == "BETA" for o in worker.store.observations(replay["id"]))
    assert len(worker.store.captures(replay["id"])) == 1


def test_old_snapshot_replay_never_supersedes_newer_retrieval(engine, source):
    spec = job_spec(source, "/records")
    first = engine.run(engine.submit(spec))
    old = engine.store.captures(first["id"])[0]
    source["version"] = 2
    engine.run(engine.submit(spec))
    worker = offline_engine(engine)
    worker.run(worker.replay(ReplaySpec(capture_ids=[old["id"]])))
    alpha = next(e for e in worker.store.canonical("default") if "alpha" in e["entity_key"])
    assert alpha["fields"]["size"]["value"] == 2
    assert not alpha["source_states"][0]["stale"]


def test_checkpoint_survives_cancel_and_frontier_limit(engine, source):
    spec = job_spec(source)
    spec.limits.tasks = 1
    result = engine.run(engine.submit(spec))
    assert result["status"] == "partial" and result["captures"] == 1
    assert engine.store.extractions(result["id"]) == []
    replay = engine.replay(ReplaySpec(capture_ids=[1]))
    assert engine.run(replay)["status"] == "completed"
    job = engine.submit(job_spec(source))
    engine.step(job)
    task = engine.store.claim(job)
    engine.store.stop(job)
    with pytest.raises(LostLease):
        engine.process(task)
    assert engine.store.captures(job)
    assert engine.store.extractions(job)[0]["status"] == "cancelled"


def test_extraction_lease_fencing_does_not_repeat_get(engine, source):
    job = engine.submit(job_spec(source))
    engine.step(job)
    first = engine.store.claim(job, lease_seconds=0.001)
    time.sleep(0.01)
    second = engine.store.claim(job)
    with pytest.raises(LostLease):
        engine.process(first)
    engine.process(second)
    assert engine.run(job)["status"] == "completed"
    assert source["counts"]["/page2"] == 1
    assert len(engine.store.extractions(job)) == 1


def test_replay_api_auth_validation_history_and_pagination(engine, source):
    original = engine.run(engine.submit(job_spec(source)))
    client = TestClient(create_app(engine.settings))
    headers = {
        "Authorization": "Bearer " + engine.settings.api_token,
        "Idempotency-Key": "api-replay",
    }
    assert client.post("/replays", json={"capture_ids": [1]}).status_code == 401
    assert client.post("/replays", headers=headers, json={"capture_ids": [999]}).status_code == 404
    for ids in ([], [True], [-1], ["1"]):
        assert (
            client.post("/replays", headers=headers, json={"capture_ids": ids}).status_code == 422
        )
    response = client.post("/replays", headers=headers, json={"capture_ids": [1]})
    assert response.status_code == 202
    job = response.json()["id"]
    assert engine.run(job)["requests"] == 0
    assert (
        client.get(f"/jobs/{job}/captures", headers=headers).json()[0]["job_id"] == original["id"]
    )
    assert client.get(f"/jobs/{job}/captures?after=1", headers=headers).json() == []
    extraction = client.get(f"/jobs/{job}/extractions", headers=headers).json()[0]
    assert extraction["status"] == "complete"
    assert (
        client.get(f"/jobs/{job}/extractions?after={extraction['id']}", headers=headers).json()
        == []
    )
    assert '"extraction_ids"' in client.get(f"/jobs/{job}/export", headers=headers).text


def test_replay_cli_runs_without_source_contact(engine, source):
    engine.run(engine.submit(job_spec(source)))
    count = len(source["requests"])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "harvest.cli",
            "--db",
            engine.store.path,
            "replay",
            "1",
            "--key",
            "cli-replay",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["requests"] == 0
    assert len(source["requests"]) == count


def make_schema_one(path):
    # SCHEMA intentionally remains the immutable v1 bootstrap; migrations follow it.
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        spec = packed(
            JobSpec(objective="Legacy evidence", seeds=["https://example.org/data"]).model_dump()
        )
        # 0.1 did not validate defaults: integer spellings round-tripped as floats.
        legacy_spec = json.loads(spec)
        legacy_spec["limits"]["cost_usd"] = 2
        legacy_spec["limits"]["domain_delay"] = 1
        spec = packed(legacy_spec)
        db.execute(
            "INSERT INTO jobs(id,spec,spec_hash,idempotency_key,status,created) VALUES('old',?,?,?,'completed',1)",
            (spec, digest(spec), "old-key"),
        )
        db.execute(
            "INSERT INTO tasks(id,job_id,kind,key,payload,depth,priority,reason,status) VALUES(1,'old','fetch','https://example.org/data','{}',0,0,'seed','done')"
        )
        body = b'{"name":"Legacy"}'
        db.execute("INSERT INTO blobs VALUES(?,?)", (digest(body), body))
        db.execute(
            "INSERT INTO captures VALUES(1,'old',1,'https://example.org/data','https://example.org/data',1,200,?, ?,NULL,1,'json/1','default')",
            (packed({"content-type": "application/json"}), digest(body)),
        )
        db.execute("INSERT INTO entities VALUES('e','default','legacy')")
        db.execute(
            "INSERT INTO observations VALUES('o','e','name','\"Legacy\"','https://example.org/data','\"Legacy\"','/name','structured','json/1',1,1)"
        )
        db.execute("INSERT INTO sightings VALUES('o',1)")
        db.execute("PRAGMA user_version=1")


def test_schema_one_migration_preserves_data_keys_and_backup(tmp_path):
    path = tmp_path / "legacy.sqlite"
    make_schema_one(path)
    store = Store(path)
    assert store.capture(1)["body"] == b'{"name":"Legacy"}'
    assert store.observations("old")[0]["value"] == "Legacy"
    assert store.canonical("default")[0]["fields"]["name"]["value"] == "Legacy"
    spec = JobSpec.model_validate(store.job("old")["spec"])
    assert store.create(spec, [], "old-key") == "old"
    with store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backup = tmp_path / "backup.sqlite"
    store.backup(backup)
    assert Store(backup).extractions("old") == store.extractions("old")
    assert Store(path).observations("old") == store.observations("old")


def test_migration_failure_rolls_back_ddl_and_can_restart(tmp_path):
    path = tmp_path / "legacy.sqlite"
    make_schema_one(path)

    class FailingMigration(Store):
        @contextmanager
        def transaction(self):
            with super().transaction() as db:
                yield db
                raise RuntimeError("migration fault")

    with pytest.raises(RuntimeError, match="migration fault"):
        FailingMigration(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "execution" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='extractions'").fetchall()
    with ThreadPoolExecutor(2) as pool:
        stores = list(pool.map(Store, [path, path]))
    assert stores[0].observations("old") == stores[1].observations("old")


def test_schema_one_pending_reason_task_resumes_in_original_revision(tmp_path, engine, source):
    path = tmp_path / "pending-legacy.sqlite"
    make_schema_one(path)
    body = b"<title>Legacy</title><p>Alpha supports HTTP/2.</p>"
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO blobs VALUES(?,?)", (digest(body), body))
        db.execute(
            "UPDATE captures SET body_hash=?,headers=?",
            (digest(body), packed({"content-type": "text/html"})),
        )
        db.execute("UPDATE jobs SET status='running'")
        db.execute(
            "INSERT INTO tasks(id,job_id,kind,key,payload,depth,priority,reason,status,parent) VALUES(2,'old','reason','1',?,0,100,'legacy reasoning','pending',1)",
            (packed({"capture_id": 1}),),
        )
    settings = engine.settings
    settings.database = str(path)
    settings.model_url = source["base"] + "/v1"
    settings.model_name = "fixture-model"
    settings.model_usd_per_million = 0
    worker = Engine(settings)
    assert worker.run("old")["status"] == "completed"
    assert any(o["value"] == "HTTP/2" for o in worker.store.observations("old"))
    assert len(worker.store.extractions("old")) == 1


def test_abrupt_process_exit_after_checkpoint_never_refetches(engine, source):
    job = engine.submit(job_spec(source))
    code = """
import os,sys
from harvest.config import Settings
from harvest.engine import Engine
engine=Engine(Settings(database=sys.argv[1],private_hosts=frozenset({'127.0.0.1'}),proxy=None))
engine.step(sys.argv[2])
os._exit(27)
"""
    result = subprocess.run([sys.executable, "-c", code, engine.store.path, job], timeout=10)
    assert result.returncode == 27
    assert engine.store.extractions(job)[0]["status"] == "pending"
    assert Engine(engine.settings).run(job)["status"] == "completed"
    assert source["counts"]["/page2"] == 1


def test_failed_replay_keeps_old_values_and_exposes_failed_revision(engine, source):
    job = engine.run(engine.submit(job_spec(source)))
    worker = offline_engine(engine)
    worker.extractors.adapters = []
    replay = worker.run(worker.replay(ReplaySpec(capture_ids=[1])))
    assert replay["status"] == "failed"
    entity = worker.store.canonical("default")[0]
    assert entity["fields"]["name"]["value"] == "Beta"
    assert entity["source_states"][0]["status"] == "failed"
    assert entity["source_states"][0]["stale"]
    assert worker.store.observations(job["id"])


def test_replay_rejects_cross_dataset_selection_and_insufficient_tasks(engine, source):
    engine.run(engine.submit(job_spec(source, dataset="one")))
    engine.run(engine.submit(job_spec(source, dataset="two")))
    with pytest.raises(ValueError, match="one dataset"):
        engine.replay(ReplaySpec(capture_ids=[1, 2]))
    engine.run(engine.submit(job_spec(source, dataset="one")))
    with pytest.raises(ValueError, match="every selected"):
        engine.replay(ReplaySpec(capture_ids=[1, 3], limits=Limits(tasks=1)))


def test_offline_replay_does_not_tick_existing_refresh_schedules(engine, source):
    first = engine.run(engine.submit(job_spec(source, mode="continuous", refresh_seconds=60)))
    replay = engine.replay(ReplaySpec(capture_ids=[1]))
    with engine.store.transaction() as db:
        db.execute("UPDATE schedules SET next_run=0")
    assert engine.run(replay)["status"] == "completed"
    assert len(engine.store.jobs()) == 2
    engine.store.disable_schedule(first["id"])


def test_pending_new_capture_does_not_clear_previous_values(engine, source):
    spec = job_spec(source, "/records")
    engine.run(engine.submit(spec))
    source["version"] = 2
    job = engine.submit(spec)
    engine.step(job)
    alpha = next(e for e in engine.store.canonical("default") if "alpha" in e["entity_key"])
    assert alpha["fields"]["size"]["value"] == 1
    assert alpha["source_states"][0]["status"] == "pending"
    assert alpha["source_states"][0]["stale"]
    engine.run(job)
    alpha = next(e for e in engine.store.canonical("default") if "alpha" in e["entity_key"])
    assert alpha["fields"]["size"]["value"] == 2
    assert not alpha["source_states"][0]["stale"]


def test_replay_cannot_execute_injected_network_task(engine, source):
    engine.run(engine.submit(job_spec(source)))
    replay = engine.replay(ReplaySpec(capture_ids=[1]))
    with engine.store.transaction() as db:
        spec = JobSpec.model_validate(engine.store.job(replay)["spec"])
        engine.store.enqueue(
            db,
            replay,
            {"kind": "fetch", "key": "injected", "payload": {"url": source["base"] + "/private"}},
            spec,
        )
    before = len(source["requests"])
    result = engine.run(replay)
    assert result["status"] == "partial" and result["progress"]["blocked"] == 1
    assert len(source["requests"]) == before and result["requests"] == 0
