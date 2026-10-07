import json

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
