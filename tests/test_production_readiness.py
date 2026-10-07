import json
import time

from harvest.models import JobSpec
from harvest.network import Capture
from harvest.planning import (
    DEFAULT_INVESTIGATION_PRESET,
    investigation_presets,
    preset_settings,
)
from harvest.store import Store


def _tool_task(tool="maigret", target="janedoe"):
    return {
        "kind": "tool",
        "key": f"{tool}:{target}",
        "payload": {"tool": tool, "target": target, "crawl": False},
        "depth": 0,
        "priority": 0,
        "reason": "test",
    }


def test_server_owns_thorough_preset_defaults():
    presets = investigation_presets()
    assert DEFAULT_INVESTIGATION_PRESET == "deep"
    thorough = presets["deep"]
    assert thorough["label"] == "Thorough"
    assert thorough["requests"] == 300
    assert thorough["seconds"] == 3600
    assert thorough["pivots"] == 3
    assert thorough["tool_runs"] == 8
    assert thorough["followup_top_sites"] is None
    assert preset_settings("standard")["followup_top_sites"] == 500


def test_tool_execution_native_artifact_is_separate_and_durable(tmp_path):
    store = Store(tmp_path / "harvest.sqlite")
    spec = JobSpec(objective="test tool execution")
    job_id = store.create(spec, [_tool_task()])
    task = store.claim(job_id)
    assert task is not None
    store.start_tool_execution(
        task,
        tool="maigret",
        target="janedoe",
        settings={"timeout_seconds": 30, "top_sites": 100},
        version="0.6.6",
    )
    native = json.dumps({"GitHub": {"status": "claimed"}}).encode()
    normalized = json.dumps(
        {"tool": "maigret", "target": "janedoe", "results": [{"url": "https://github.com/janedoe"}]}
    ).encode()
    cap = Capture(
        url="tool://maigret/janedoe",
        final_url="tool://maigret/janedoe",
        status=200,
        headers={"content-type": "application/json"},
        body=normalized,
        retrieved=1.0,
        native_body=native,
        tool_meta={
            "version": "0.6.6",
            "checks": {
                "found": 1,
                "absent": 0,
                "blocked": 0,
                "errored": 0,
                "unsupported": 0,
                "skipped": 0,
                "unfinished": 0,
            },
            "diagnostics": {"coverage": "unknown"},
        },
    )
    capture_id = store.finish(task, response=cap)
    executions = store.tool_executions(job_id)
    assert len(executions) == 1
    execution = executions[0]
    assert execution["outcome"] == "complete"
    assert execution["capture_id"] == capture_id
    assert execution["settings"]["top_sites"] == 100
    assert execution["checks"]["found"] == 1
    assert execution["diagnostics"]["coverage"] == "unknown"
    artifact = store.tool_artifact(execution["id"], "native-structured")
    assert artifact["body"] == native
    assert artifact["body_hash"] != store.capture(capture_id)["body_hash"]


def test_tool_execution_worker_loss_is_interrupted_not_replayed(tmp_path):
    store = Store(tmp_path / "harvest.sqlite")
    spec = JobSpec(objective="test interrupted tool")
    job_id = store.create(spec, [_tool_task()])
    task = store.claim(job_id, lease_seconds=10)
    assert task is not None
    store.start_tool_execution(
        task,
        tool="maigret",
        target="janedoe",
        settings={"timeout_seconds": 30},
    )
    with store.transaction() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    assert store.claim(job_id) is None
    execution = store.tool_executions(job_id)[0]
    assert execution["outcome"] == "interrupted"
    with store.connection() as db:
        row = db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
    assert row["status"] == "failed"


def _fetch_task(key):
    return {
        "kind": "fetch",
        "key": key,
        "payload": {"url": f"https://example.org/{key}"},
        "depth": 0,
        "priority": 0,
        "reason": "test",
    }


def test_shared_admission_limits_two_investigations(tmp_path):
    store = Store(tmp_path / "harvest.sqlite")
    jobs = [store.create(JobSpec(objective=f"job {i}"), [_fetch_task(f"p{i}")]) for i in range(3)]
    assert store.claim(jobs[0], max_active_investigations=2, max_running_tools=2)
    assert store.claim(jobs[1], max_active_investigations=2, max_running_tools=2)
    assert store.claim(jobs[2], max_active_investigations=2, max_running_tools=2) is None


def test_shared_admission_limits_two_tool_tasks(tmp_path):
    store = Store(tmp_path / "harvest.sqlite")
    jobs = [
        store.create(JobSpec(objective=f"tool job {i}"), [_tool_task(target=f"user{i}")])
        for i in range(3)
    ]
    assert store.claim(jobs[0], max_active_investigations=10, max_running_tools=2)
    assert store.claim(jobs[1], max_active_investigations=10, max_running_tools=2)
    assert store.claim(jobs[2], max_active_investigations=10, max_running_tools=2) is None


def test_incidents_record_transitions_without_duplicate_alerts(tmp_path):
    store = Store(tmp_path / "harvest.sqlite")
    first, changed = store.set_incident(
        "readiness:worker", active=True, details={"age_seconds": 120}
    )
    assert changed is True
    again, changed = store.set_incident(
        "readiness:worker", active=True, details={"age_seconds": 135}
    )
    assert again == first and changed is False
    resolved, changed = store.set_incident(
        "readiness:worker", active=False, details={"age_seconds": 0}
    )
    assert resolved == first and changed is True
    rows = store.incidents()
    assert len(rows) == 1 and rows[0]["resolved"] is not None


def test_readiness_requires_worker_and_verified_offhost_backup(engine):
    initial = engine.readiness()
    assert initial["ready"] is False
    assert initial["checks"]["worker"]["ok"] is False
    assert initial["checks"]["backup"]["ok"] is False

    engine.store.heartbeat_service("worker", {"tools": {}})
    engine.store.record_backup(
        kind="production",
        manifest_hash="a" * 64,
        verified=True,
        offhost_at=time.time(),
    )
    ready = engine.readiness(record_incidents=True)
    assert ready["ready"] is True
    assert ready["checks"]["worker"]["ok"] is True
    assert ready["checks"]["backup"]["ok"] is True
    assert not [row for row in engine.store.incidents() if row["resolved"] is None]
