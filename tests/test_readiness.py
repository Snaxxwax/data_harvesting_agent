import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harvest import executions, operations, planning, tools
from harvest.api import create_app
from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import JobSpec
from harvest.store import Store


@pytest.fixture
def engine(tmp_path):
    # These are offline state-machine checks; no HTTP fixture server is needed.
    return Engine(
        Settings(
            database=str(tmp_path / "harvest.sqlite"),
            proxy=None,
            api_token="fixture-token-at-least-24-characters",
        )
    )


def tool_job(engine, name="maigret", target="audit_handle"):
    engine.settings.tools = frozenset({"maigret", "ghunt", "spiderfoot"})
    return engine.submit(
        JobSpec(
            objective="fixture scan",
            tools=[{"name": name, "target": target, "crawl": False}],
            limits={"seconds": 3600, "tool_runs": 8, "pivots": 3, "followup_top_sites": None},
        )
    )


def test_transactional_migration_and_legacy_unknown(engine):
    job = tool_job(engine)
    with engine.store.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        original = db.execute("SELECT spec FROM jobs WHERE id=?", (job,)).fetchone()[0]
    reopened = Store(engine.store.path)
    assert executions.coverage(reopened, job)["executions"][0]["coverage"] == "planned"
    with reopened.transaction() as db:
        db.execute("UPDATE tasks SET status='done' WHERE job_id=?", (job,))
    assert executions.coverage(reopened, job)["executions"][0]["coverage"] == "unknown"
    with reopened.connection() as db:
        assert db.execute("SELECT spec FROM jobs WHERE id=?", (job,)).fetchone()[0] == original


def test_worker_death_keeps_native_and_normalized_checkpoint_never_reexecutes(engine):
    job = tool_job(engine)
    task = engine.store.claim(job)
    with engine.store.connection() as db:
        assert (
            db.execute("SELECT lease_until FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            - time.time()
            <= 30
        )
    recorder = executions.begin(
        engine.store, task, {"proxy": "http://user:secret@proxy:8888"}, {"version": "test"}
    )
    recorder.path.write_text(
        json.dumps(
            {
                "tool": "maigret",
                "name": "FixtureSite",
                "outcome": "found",
                "native": {
                    "username": "audit_handle",
                    "url_user": "https://example.test/audit_handle",
                    "http_status": 200,
                    "site": {"checkType": "message"},
                    "status": {"status": "Claimed", "ids": {"fullname": "Audit Person"}},
                },
            }
        )
        + "\n"
        + '{"truncated":'
    )
    with engine.store.transaction() as db:
        db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, task["id"]))
    executions.recover(engine.store)
    assert engine.store.claim(job) is None
    coverage = executions.coverage(engine.store, job)
    run = coverage["executions"][0]
    assert run["outcome"] == "interrupted"
    captures = engine.store.captures(job)
    assert len(captures) == 1
    body = json.loads(engine.store.capture(captures[0]["id"])["body"])
    assert body["results"][0]["fullname"] == "Audit Person"
    assert run["counts"] == {"found": 1}
    assert run["checks"][0]["outcome"] == "found"
    with engine.store.connection() as db:
        data = b" ".join(r[0] for r in db.execute("SELECT body FROM blobs"))
    assert b"Audit Person" in data
    assert "secret" not in json.dumps(run["settings"])


def test_malformed_checkpoint_keeps_later_findings_and_missing_checks_are_unfinished(engine):
    job = tool_job(engine)
    task = engine.store.claim(job)
    rec = executions.begin(engine.store, task, {}, {})
    rec.path.write_text(
        json.dumps({"kind": "plan", "name": "eligible-sites", "native": {"sites": ["A", "B"]}})
        + '\n{"not valid":\n'
        + json.dumps({"tool": "maigret", "name": "A", "outcome": "absent", "native": {}})
        + "\n"
    )
    rec.finish("completed")
    run = executions.coverage(engine.store, job)["executions"][0]
    assert run["outcome"] == "partial"
    assert run["execution_completed"] is True
    assert run["counts"] == {"absent": 1, "errored": 1, "unfinished": 1}


def test_orphan_recovery_paginates_and_respects_database_namespace(monkeypatch):
    import httpx

    stopped, pages = [], []

    def handler(request):
        if request.method == "POST":
            stopped.append(request.url.path)
            return httpx.Response(200, json={})
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "items": [{"id": "production", "name": "harvest-t1-person", "status": "RUNNING"}]
                if page == 1
                else [
                    {"id": "alive", "name": "harvest.audit-t1-person", "status": "RUNNING"},
                    {"id": "dead", "name": "harvest.audit-t2-person", "status": "RUNNING"},
                ],
                "has_next": page == 1,
            },
        )

    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: client(**kw, transport=httpx.MockTransport(handler))
    )
    settings = Settings(
        scan_namespace="audit", spiderfoot_url="http://sf.test", spiderfoot_api_key="fixture"
    )
    assert tools.stop_orphaned_spiderfoot_scans(settings, {1}) == 1
    assert pages == [1, 2]
    assert stopped == ["/api/v1/scans/dead/stop"]


def test_spiderfoot_keeps_completed_pages_when_a_later_page_is_malformed(engine):
    import httpx

    from harvest.models import ActionableError

    job = tool_job(engine, "spiderfoot", "audit.person@example.test")
    task = engine.store.claim(job)
    recorder = executions.begin(engine.store, task, {}, {})

    def handler(request):
        if request.url.params.get("page") == "1":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {"type": "USERNAME", "data": "audit_handle", "module": "sfp_accounts"}
                    ],
                    "has_next": True,
                },
            )
        return httpx.Response(200, json={"events": "malformed"})

    with httpx.Client(base_url="http://sf.test", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ActionableError, match="contract mismatch"):
            tools._spiderfoot_snapshot(client, "scan", "audit.person@example.test", recorder)
    assert recorder.results("spiderfoot", "audit.person@example.test")[0]["data"] == "audit_handle"


def test_two_investigations_and_two_tools_admitted_across_workers(engine):
    jobs = [tool_job(engine, target=f"audit_{i}") for i in range(3)]
    tasks = [engine.store.claim(jobs[i]) for i in range(2)]
    assert all(tasks)
    assert engine.store.claim(jobs[2]) is None
    assert engine.store.job(jobs[2])["started"] is None
    engine.store.stop(jobs[0])
    assert engine.store.claim(jobs[2])


def test_low_disk_blocks_new_admission_without_deleting_jobs(engine, monkeypatch):
    from collections import namedtuple

    disk = namedtuple("disk", "total used free")
    job = tool_job(engine)
    monkeypatch.setattr("harvest.store.shutil.disk_usage", lambda _: disk(100, 91, 9))
    assert engine.store.claim(job) is None
    assert engine.store.job(job)["status"] == "queued"


def test_email_followup_uses_all_sites_and_shared_budget(engine, monkeypatch):
    calls = []
    engine.settings.spiderfoot_modules = ("sfp_accounts",)
    native = json.loads(
        (Path(__file__).parent / "fixtures/parity/spiderfoot-harvest.json").read_text()
    )

    def spiderfoot(target, workdir, timeout, cancelled, settings, **kwargs):
        calls.append(("spiderfoot", target))
        tools._RECORDER.get().check("fixture-module", "found", native=native)
        return {"results": tools._spiderfoot_records(native["events"], target)}

    def maigret(target, workdir, timeout, cancelled, settings, top_sites=None):
        calls.append(("maigret", target, top_sites))
        tools._RECORDER.get().check("fixture-site", "absent")
        return []

    monkeypatch.setitem(tools.TOOLS["spiderfoot"], "run", spiderfoot)
    monkeypatch.setitem(tools.TOOLS["maigret"], "run", maigret)
    root = tool_job(engine, "spiderfoot", "audit.person@example.test")
    engine.run(root)
    child = engine.store.investigation(root)["pivots"][0]["child"]
    engine.run(child)
    assert calls == [("spiderfoot", "audit.person@example.test"), ("maigret", "audit.person", None)]
    assert engine.store.job(child)["root_id"] == root
    assert engine.store.investigation(root)["enforced"]["tool_runs"] == {"used": 2, "limit": 8}


def test_artifact_download_requires_auth_and_job_membership(engine):
    job = tool_job(engine)
    task = engine.store.claim(job)
    recorder = executions.begin(engine.store, task, {}, {})
    aid = recorder.artifact("native", "profile", {"token": "private", "nested": {"name": "Audit"}})
    client = TestClient(create_app(engine.settings))
    path = f"/jobs/{job}/artifacts/{aid}"
    assert client.get(path).status_code == 401
    headers = {"Authorization": "Bearer " + engine.settings.api_token}
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"token": "[REDACTED]", "nested": {"name": "Audit"}}
    other = tool_job(engine, target="someone_else")
    assert client.get(f"/jobs/{other}/artifacts/{aid}", headers=headers).status_code == 404


def test_incident_transition_suppression_and_recovery(engine):
    for _ in range(3):
        operations.incident(engine.store, "proxy", False, "unreachable")
    operations.incident(engine.store, "proxy", True, "recovered")
    with engine.store.connection() as db:
        rows = db.execute("SELECT * FROM incidents").fetchall()
    assert len(rows) == 1
    assert rows[0]["resolved"]


def test_provider_failures_and_irrelevant_success_remain_degraded():
    from harvest.search import known_answer

    result = known_answer(
        {
            "results": [
                {
                    "url": "https://www.iana.org/domains/reserved",
                    "title": "Reserved domains",
                    "engines": ["duckduckgo"],
                },
                {
                    "url": "https://example.test/irrelevant",
                    "title": "Unrelated page",
                    "engine": "bing",
                },
            ],
            "unresponsive_engines": [
                ["brave", "too many requests"],
                ["parser", "invalid response"],
            ],
        }
    )
    providers = {r["engine"]: r for r in result["providers"]}
    assert result["ready"] is True
    assert providers["duckduckgo"]["accepted"] == 1
    assert providers["bing"]["outcome"] == "errored"
    assert providers["bing"]["rejected"] == 1
    assert providers["brave"]["outcome"] == "blocked"
    assert providers["parser"]["reason"] == "invalid response"


def test_all_search_providers_blocked_keeps_capture_and_reason(engine, monkeypatch):
    from harvest.network import Capture

    engine.settings.search_url = "http://search.test"
    payload = {"results": [], "unresponsive_engines": [["brave", "CAPTCHA"]]}

    def raw_get(self, url, **kwargs):
        return Capture(
            url=url,
            final_url=url,
            status=200,
            headers={"content-type": "application/json"},
            body=json.dumps(payload).encode(),
            retrieved=time.time(),
        )

    monkeypatch.setattr("harvest.engine.Fetcher.raw_get", raw_get)
    job = engine.submit(JobSpec(objective="fixture search", discovery_queries=["fixture"]))
    result = engine.run(job)
    assert result["status"] == "partial"
    capture = engine.store.captures(job)[0]
    assert json.loads(engine.store.capture(capture["id"])["body"]) == payload
    provider = engine.store.job_summary(job)["search"]["providers"][0]
    assert provider == {
        "engine": "brave",
        "outcome": "blocked",
        "reason": "CAPTCHA",
        "accepted": 0,
        "rejected": 0,
    }


def test_ghunt_native_fields_survive_normalization():
    native = json.loads((Path(__file__).parent / "fixtures/parity/ghunt-native.json").read_text())
    records = tools._ghunt_records(native, "audit.person@example.test")
    assert len(records) == 1
    for key, value in native["PROFILE_CONTAINER"].items():
        assert records[0][key] == value
    assert (
        tools._ghunt_records({"PROFILE_CONTAINER": {"profile": {}}}, "audit.person@example.test")
        == []
    )


def test_thorough_is_server_default_explicit_spec_defaults_unchanged(engine):
    plan = planning.effective_plan("audit.person@example.test", settings=engine.settings)
    assert plan["preset"] == "deep"
    assert plan["limits"] == {
        "requests": 300,
        "seconds": 3600,
        "pivots": 3,
        "tool_runs": 8,
        "followup_top_sites": None,
    }
    assert plan["coverage"]["maigret_breadth"] == "all eligible sites"
    assert JobSpec(objective="legacy", seeds=["https://example.test"]).limits.pivots == 0
    with pytest.raises(ValueError):
        planning.effective_plan(
            "audit.person@example.test", preset="missing", settings=engine.settings
        )
