import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from harvest import tools
from harvest.config import Settings
from harvest.models import JobSpec, PolicyDenied, ReplaySpec
from harvest.planning import plan_investigation

MAIGRET_SIMPLE_REPORT = {
    "GitHub": {
        "url_user": "https://github.com/janedoe",
        "status": {"status": "claimed"},
        "site": {"name": "GitHub", "tags": ["coding"]},
    },
    "Reddit": {
        "url_user": "https://reddit.com/user/janedoe",
        "status": {"status": "claimed"},
        "site": {"name": "Reddit"},
    },
}


@pytest.fixture
def fake_maigret(monkeypatch):
    """Stand in for the binary, but exercise the real report-file contract: maigret writes
    report_<username>_simple.json into --folderoutput and prints nothing useful to stdout."""
    calls = []

    def fake_exec(argv, timeout, cwd, cancelled=None):
        calls.append(argv)
        workdir = argv[argv.index("--folderoutput") + 1]
        target = argv[1]
        report = f"{workdir}/report_{target}_simple.json"
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(MAIGRET_SIMPLE_REPORT, handle)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(tools, "_exec", fake_exec)
    return calls


class _Created(list):
    """A list that can also carry the signals the fake process group received."""

    signalled: list


class FakePopen:
    """Stands in for subprocess.Popen so the tests drive the one real execution path.

    Patching subprocess.run would silently test nothing: production always goes through
    Popen so the tool can be terminated mid-scan.
    """

    def __init__(self, argv, returncode=0, on_start=None, hang=False, **kwargs):
        self.args = argv
        self.pid = -1
        self.returncode = None
        self._final = returncode
        self._hang = hang
        if on_start:
            on_start(argv, kwargs.get("cwd"))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        # A hanging process still returns once it has been signalled, which is what
        # _stop_process relies on: it retries communicate() with no timeout after SIGKILL.
        if self._hang and timeout is not None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = self._final
        return b"", b""


def popen_factory(monkeypatch, **kwargs):
    """Patch Popen and neutralise os.killpg. The fake has no real process group, and
    killpg on a fake pid would signal something real; the genuine terminate-the-group
    behaviour is covered by test_cancelling_job_terminates_running_tool instead."""
    created = _Created()
    signalled = []

    def factory(argv, **popen_kwargs):
        created.append(argv)
        return FakePopen(argv, **kwargs, **popen_kwargs)

    monkeypatch.setattr(subprocess, "Popen", factory)
    monkeypatch.setattr(tools.os, "killpg", lambda pid, sig: signalled.append(sig))
    created.signalled = signalled
    return created


def enabled(**kwargs):
    return Settings(tools=frozenset({"maigret"}), **kwargs)


def test_tool_is_denied_unless_allowlisted():
    with pytest.raises(PolicyDenied):
        tools.run("maigret", "janedoe", Settings(tools=frozenset()))


def test_unknown_tool_rejected():
    with pytest.raises(ValueError, match="unknown tool"):
        tools.run("nope", "janedoe", enabled())


@pytest.mark.parametrize("target", ["--all-sites", "-a", "jane doe", "jane/doe", "", "a" * 300])
def test_target_cannot_become_a_flag_or_path(target):
    with pytest.raises(ValueError, match="tool target"):
        tools.run("maigret", target, enabled())


def test_capture_flattens_one_record_per_claimed_account(fake_maigret):
    capture = tools.run("maigret", "janedoe", enabled())

    assert capture.url == capture.final_url == "tool://maigret/janedoe"
    assert capture.headers["content-type"] == "application/json"
    body = json.loads(capture.body)
    assert body["tool"] == "maigret" and body["target"] == "janedoe"
    # One record per account, each carrying a crawlable `url`, not one giant nested record.
    assert [r["sitename"] for r in body["results"]] == ["GitHub", "Reddit"]
    assert [r["url"] for r in body["results"]] == [
        "https://github.com/janedoe",
        "https://reddit.com/user/janedoe",
    ]
    argv = fake_maigret[0]
    assert argv[0] == "maigret" and argv[1] == "janedoe"
    assert "--no-progressbar" in argv and "--json" in argv
    # The full site set is the point of the integration; losing this flag silently cuts
    # coverage by about ten times without any other visible change.
    assert "--all-sites" in argv


def test_clean_exit_without_a_report_is_an_error_not_an_empty_capture(monkeypatch):
    """Exit 0 and no report file is the only way this path is now reachable; a nonzero exit
    is rejected earlier, in _exec."""
    popen_factory(monkeypatch)
    with pytest.raises(ValueError, match="no JSON report"):
        tools.run("maigret", "janedoe", enabled())


def test_timeout_fails_permanently_rather_than_retrying(monkeypatch):
    """A retried scan would cost another few thousand unbudgeted third-party requests, so
    limits.tool_runs must bound invocations and not merely declarations."""

    popen_factory(monkeypatch, hang=True)
    with pytest.raises(ValueError, match="exceeded"):
        tools.run("maigret", "janedoe", enabled(tool_timeout=0.3))


def test_nonzero_exit_is_not_ingested_even_with_a_report(monkeypatch):
    """maigret only exits nonzero on startup/config failure or an interrupt, so a report
    left behind by an aborted run is partial and must not become a 200 capture."""

    def leave_report(argv, cwd):
        workdir = argv[argv.index("--folderoutput") + 1]
        report = Path(workdir) / "report_janedoe_simple.json"
        report.write_text(json.dumps(MAIGRET_SIMPLE_REPORT), encoding="utf-8")

    popen_factory(monkeypatch, returncode=2, on_start=leave_report)
    with pytest.raises(ValueError, match="exited 2"):
        tools.run("maigret", "janedoe", enabled())


def test_one_declared_run_invokes_the_binary_once(tmp_path, monkeypatch):
    """The end-to-end form of the same guarantee: a timing-out tool task is not retried."""
    from harvest.engine import Engine

    calls = popen_factory(monkeypatch, hang=True)
    engine = Engine(enabled(database=str(tmp_path / "h.sqlite"), tool_timeout=0.3))
    job = engine.submit(
        JobSpec(
            objective="profile janedoe",
            tools=[{"name": "maigret", "target": "janedoe"}],
            limits={"tool_runs": 1, "attempts": 3},
        )
    )
    for _ in range(8):
        with engine.store.connection() as db:
            db.execute("UPDATE tasks SET ready=0 WHERE job_id=?", (job,))
        if not engine.step(job):
            break
    assert len(calls) == 1


@pytest.mark.parametrize("target", ["janedoe\n", "bad target", "--all-sites", "jane/doe"])
def test_invalid_target_is_rejected_at_submit_not_mid_job(target):
    """A bad target used to be accepted as a job and fail later with a generic adapter
    error; ToolRun now rejects it, so the API answers 422."""
    with pytest.raises(ValidationError):
        JobSpec(objective="profile janedoe", tools=[{"name": "maigret", "target": target}])


def test_missing_binary_is_a_policy_denial(monkeypatch):
    def absent(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", absent)
    with pytest.raises(PolicyDenied, match="not installed"):
        tools.run("maigret", "janedoe", enabled())


def test_plan_suggests_maigret_for_a_username():
    plan = plan_investigation("@janedoe")
    assert plan.tools == [{"name": "maigret", "target": "janedoe"}]
    # A tool is never suggested for an input it cannot consume.
    assert plan_investigation("jane@example.org").tools == []


def test_submit_rejects_a_tool_the_deployment_has_not_enabled(tmp_path):
    from harvest.engine import Engine

    engine = Engine(Settings(database=str(tmp_path / "h.sqlite"), tools=frozenset()))
    spec = JobSpec(objective="profile janedoe", tools=[{"name": "maigret", "target": "janedoe"}])
    with pytest.raises(ValueError, match="not enabled"):
        engine.submit(spec)


def test_submit_bounds_tool_runs(tmp_path):
    from harvest.engine import Engine

    engine = Engine(enabled(database=str(tmp_path / "h.sqlite")))
    spec = JobSpec(
        objective="profile janedoe",
        tools=[{"name": "maigret", "target": f"jane{n}"} for n in range(4)],
        limits={"tool_runs": 2},
    )
    with pytest.raises(ValueError, match="exceeds limits.tool_runs"):
        engine.submit(spec)


def test_tool_output_becomes_observations_and_leads(tmp_path, fake_maigret):
    """The whole point of the seam: a tool run is an acquisition, and the existing JSON
    adapter and extract task turn it into evidence with no tool-specific parsing."""
    from harvest.engine import Engine

    engine = Engine(enabled(database=str(tmp_path / "h.sqlite")))
    spec = JobSpec(
        objective="profile the username janedoe",
        tools=[{"name": "maigret", "target": "janedoe"}],
        # No seeds and no search configured: the tool alone is enough work to start a job.
        allowed_domains=["github.com"],
    )
    job = engine.submit(spec)

    # Exactly two steps: the tool acquisition and the extraction of its capture. The queue is
    # deliberately not drained -- the leads below point at the public internet, and a unit test
    # must never acquire them (see the `live` marker for tests that do).
    assert engine.step(job) is True
    assert engine.step(job) is True

    observations = engine.store.observations(job, limit=200)
    urls = {o["value"] for o in observations if o["field"] == "url"}
    assert "https://github.com/janedoe" in urls
    assert {o["source_url"] for o in observations} == {"tool://maigret/janedoe"}
    # Claimed profiles were queued as leads, and allowed_domains still gated them.
    with engine.store.connection() as db:
        queued = {
            r[0]
            for r in db.execute("SELECT key FROM tasks WHERE job_id=? AND kind='fetch'", (job,))
        }
    assert "https://github.com/janedoe" in queued
    assert not any("reddit.com" in u for u in queued)


def test_tool_capture_can_be_replayed_offline(tmp_path, fake_maigret):
    from harvest.engine import Engine

    engine = Engine(enabled(database=str(tmp_path / "h.sqlite")))
    job = engine.submit(
        JobSpec(objective="profile janedoe", tools=[{"name": "maigret", "target": "janedoe"}])
    )
    engine.step(job)
    engine.step(job)
    capture = engine.store.captures(job)[0]
    replay = engine.replay(ReplaySpec(capture_ids=[capture["id"]]))
    result = engine.run(replay)
    assert result["status"] == "completed"
    assert result["requests"] == 0
    assert engine.store.observations(replay, limit=200)


def test_expired_tool_lease_fails_without_another_invocation(tmp_path):
    from harvest.engine import Engine

    engine = Engine(enabled(database=str(tmp_path / "h.sqlite")))
    job = engine.submit(
        JobSpec(
            objective="profile janedoe",
            tools=[{"name": "maigret", "target": "janedoe"}],
            limits={"attempts": 3, "tool_runs": 1},
        )
    )
    task = engine.store.claim(job)
    assert task["kind"] == "tool"
    with engine.store.connection() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    assert engine.store.claim(job) is None
    with engine.store.connection() as db:
        status, attempts = db.execute(
            "SELECT status,attempts FROM tasks WHERE id=?", (task["id"],)
        ).fetchone()
    assert (status, attempts) == ("failed", 1)


def test_deferred_tool_attempt_fails_instead_of_relaunching(tmp_path):
    from harvest.engine import Engine

    engine = Engine(enabled(database=str(tmp_path / "h.sqlite")))
    job = engine.submit(
        JobSpec(
            objective="profile janedoe",
            tools=[{"name": "maigret", "target": "janedoe"}],
            limits={"attempts": 3, "tool_runs": 1},
        )
    )
    task = engine.store.claim(job)
    engine.store.defer(task, OSError("storage failed after scan"), delay=0)
    assert engine.store.claim(job) is None
    with engine.store.connection() as db:
        assert (
            db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
            == "failed"
        )


def test_cancelling_job_terminates_running_tool(tmp_path, monkeypatch):
    from harvest.engine import Engine

    engine = Engine(enabled(database=str(tmp_path / "h.sqlite"), tool_timeout=10))
    job = engine.submit(
        JobSpec(objective="profile janedoe", tools=[{"name": "maigret", "target": "janedoe"}])
    )
    started = tmp_path / "started"
    finished = tmp_path / "finished"

    def slow_tool(target, workdir, timeout, cancelled):
        code = (
            "from pathlib import Path; import time; "
            f"Path({str(started)!r}).write_text('started'); "
            "time.sleep(5); "
            f"Path({str(finished)!r}).write_text('finished')"
        )
        tools._exec([sys.executable, "-c", code], timeout, workdir, cancelled)
        return []

    monkeypatch.setitem(tools.TOOLS["maigret"], "run", slow_tool)
    worker = threading.Thread(target=engine.step, args=(job,))
    worker.start()
    deadline = time.monotonic() + 4
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists()
    engine.store.stop(job)
    worker.join(timeout=4)
    assert not worker.is_alive()
    assert engine.store.job(job)["status"] == "cancelled"
    assert not finished.exists()


def _ui_client(engine):
    from fastapi.testclient import TestClient

    from harvest.api import create_app

    client = TestClient(create_app(engine.settings))
    client.post("/session", json={"token": engine.settings.api_token})
    return client


def _ui_engine(tmp_path, **kwargs):
    from harvest.engine import Engine

    return Engine(
        Settings(
            database=str(tmp_path / "h.sqlite"),
            api_token="test-operator-token-at-least-24-characters",
            **kwargs,
        )
    )


def test_meta_reports_only_enabled_and_known_tools(tmp_path):
    # "nope" is not a real tool; the allowlist must not be able to advertise one to the UI.
    engine = _ui_engine(tmp_path, tools=frozenset({"maigret", "nope"}))
    assert _ui_client(engine).get("/meta").json()["tools_enabled"] == ["maigret"]


def test_meta_reports_no_tools_by_default(tmp_path):
    engine = _ui_engine(tmp_path)
    assert _ui_client(engine).get("/meta").json()["tools_enabled"] == []


def test_ui_only_offers_tools_that_meta_reports():
    """The UI filters a plan's suggestions against /meta, so a suggestion for a tool the
    deployment never enabled can't be submitted into a guaranteed 422."""
    import harvest

    source = (Path(harvest.__file__).parent / "web" / "app.js").read_text()
    assert "toolsEnabled.includes(t.name)" in source
    # Nothing is preselected: a checkbox must be ticked before any binary runs.
    assert "box.checked" in source


def test_plan_endpoint_exposes_tool_suggestions(tmp_path):
    engine = _ui_engine(tmp_path, tools=frozenset({"maigret"}))
    body = _ui_client(engine).post("/plan/investigation", json={"value": "@janedoe"}).json()
    assert body["tools"] == [{"name": "maigret", "target": "janedoe"}]
    # A suggestion is independent of the allowlist; /meta is what gates the offer.
    assert body["kind"] == "username"


def test_job_submitted_with_a_selected_tool_enqueues_a_tool_task(tmp_path):
    engine = _ui_engine(tmp_path, tools=frozenset({"maigret"}))
    client = _ui_client(engine)
    plan = client.post("/plan/investigation", json={"value": "@janedoe"}).json()
    response = client.post(
        "/jobs",
        json={
            "objective": f"Investigate {plan['normalized']}",
            "fields": plan["fields"],
            "tools": plan["tools"],
        },
    )
    assert response.status_code == 202
    with engine.store.connection() as db:
        rows = db.execute(
            "SELECT kind,key FROM tasks WHERE job_id=?", (response.json()["id"],)
        ).fetchall()
    assert [tuple(r) for r in rows] == [("tool", "maigret:janedoe")]


def test_job_submitted_with_a_disabled_tool_is_rejected(tmp_path):
    engine = _ui_engine(tmp_path, tools=frozenset())
    response = _ui_client(engine).post(
        "/jobs",
        json={
            "objective": "Investigate janedoe",
            "tools": [{"name": "maigret", "target": "janedoe"}],
        },
    )
    assert response.status_code == 422
    assert "not enabled" in response.json()["detail"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("maigret:janedoe", {"name": "maigret", "target": "janedoe"}),
        ("MAIGRET:janedoe", {"name": "maigret", "target": "janedoe"}),
        (" maigret : janedoe ", {"name": "maigret", "target": "janedoe"}),
    ],
)
def test_cli_parses_tool_argument(value, expected):
    from harvest.cli import parse_tool

    assert parse_tool(value) == expected


def test_cli_rejects_tool_argument_without_a_target():
    from harvest.cli import parse_tool

    with pytest.raises(SystemExit, match="NAME:TARGET"):
        parse_tool("maigret")


def test_cli_investigate_submits_declared_tools(tmp_path, monkeypatch):
    """--tool reaches the spec, and the allowlist still gates it at submit."""
    from harvest import cli

    db = str(tmp_path / "h.sqlite")
    argv = ["harvest", "--db", db, "investigate", "profile janedoe", "--tool", "maigret:janedoe"]
    monkeypatch.setattr(sys, "argv", argv)

    # Consistent with the rest of this CLI: a rejected spec surfaces the ValueError rather
    # than a tidy exit, exactly as an unparseable --seed already does.
    monkeypatch.delenv("HARVEST_TOOLS", raising=False)
    with pytest.raises(ValueError, match="not enabled"):
        cli.main()

    monkeypatch.setenv("HARVEST_TOOLS", "maigret")
    monkeypatch.setattr(cli.Engine, "run", lambda self, job_id: self.store.job(job_id))
    with pytest.raises(SystemExit):
        cli.main()
    from harvest.store import Store

    with Store(db).connection() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT kind,key FROM tasks")]
    assert rows == [("tool", "maigret:janedoe")]
