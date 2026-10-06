import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from harvest import tools
from harvest.config import Settings
from harvest.models import JobSpec, LostLease, PolicyDenied, ReplaySpec
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
    calls = _Created()
    calls.envs = []

    def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
        calls.append(argv)
        calls.envs.append(env)
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
    assert argv[argv.index("--retries") + 1] == "0"
    assert "--cloudflare-bypass" not in argv


def test_maigret_optional_site_retries_and_bypass_are_explicit(fake_maigret):
    tools.run(
        "maigret",
        "janedoe",
        enabled(maigret_retries=2, maigret_cloudflare_bypass=True),
    )
    argv = fake_maigret[0]
    assert argv[argv.index("--retries") + 1] == "2"
    assert "--cloudflare-bypass" in argv


@pytest.mark.parametrize("retries", [-1, 4])
def test_invalid_maigret_retries_fail_before_launch(fake_maigret, retries):
    with pytest.raises(PolicyDenied, match="HARVEST_MAIGRET_RETRIES"):
        tools.run("maigret", "janedoe", enabled(maigret_retries=retries))
    assert not fake_maigret


def test_maigret_options_read_environment(monkeypatch):
    monkeypatch.setenv("HARVEST_MAIGRET_RETRIES", "1")
    monkeypatch.setenv("HARVEST_MAIGRET_CLOUDFLARE_BYPASS", "true")
    settings = enabled()
    assert settings.maigret_retries == 1
    assert settings.maigret_cloudflare_bypass is True


@pytest.mark.parametrize("proxy", ["http://proxy.example:8080", "http://proxy.example"])
def test_maigret_uses_configured_http_proxy_for_main_requests(fake_maigret, monkeypatch, proxy):
    monkeypatch.setenv("NO_PROXY", "*")
    tools.run("maigret", "janedoe", enabled(proxy=proxy))
    argv, env = fake_maigret[0], fake_maigret.envs[0]
    assert argv[argv.index("--proxy") + 1] == proxy
    assert "--no-autoupdate" in argv
    assert "HTTP_PROXY" not in env and "HTTPS_PROXY" not in env
    assert "NO_PROXY" not in env


def test_maigret_socks_proxy_uses_explicit_flag_without_ambient_env(fake_maigret):
    proxy = "socks5://127.0.0.1:9050"
    tools.run("maigret", "janedoe", enabled(proxy=proxy))
    argv, env = fake_maigret[0], fake_maigret.envs[0]
    assert argv[argv.index("--proxy") + 1] == proxy
    assert "--no-autoupdate" in argv
    assert "HTTPS_PROXY" not in env


def test_no_configured_proxy_does_not_inherit_ambient_proxy(fake_maigret, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.example:8080")
    tools.run("maigret", "janedoe", enabled(proxy=None))
    assert "--proxy" not in fake_maigret[0]
    # --no-autoupdate is unconditional: the site database is pinned to the image so a
    # scan's breadth is reproducible and no unbudgeted GitHub request precedes it.
    assert "--no-autoupdate" in fake_maigret[0]
    assert "HTTPS_PROXY" not in fake_maigret.envs[0]


def test_tool_environment_reaches_the_real_subprocess(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "*")
    env = tools._tool_env("http://proxy.example:8080")
    result = tools._exec(
        [
            sys.executable,
            "-c",
            "import os; print('NO_PROXY' in os.environ, 'HTTPS_PROXY' in os.environ)",
        ],
        5,
        str(tmp_path),
        env=env,
    )
    assert result.stdout.decode().strip() == "False False"


def test_invalid_tool_proxy_fails_before_launch(fake_maigret):
    with pytest.raises(PolicyDenied, match="HARVEST_EGRESS_PROXY"):
        tools.run("maigret", "janedoe", enabled(proxy="not-a-proxy"))
    assert not fake_maigret


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
    with pytest.raises(ValueError, match="exited with status 2"):
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
    # The static planner names every tool that accepts the kind; the API filters to ready ones.
    assert plan.tools == [
        {"name": "maigret", "target": "janedoe"},
        {"name": "spiderfoot", "target": "janedoe"},
    ]
    # A tool is never suggested for an input it cannot consume: maigret takes a username,
    # so an email plan must not offer it (it may offer email-consuming tools instead).
    assert "maigret" not in {t["name"] for t in plan_investigation("jane@example.org").tools}


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

    def slow_tool(target, workdir, timeout, cancelled, settings):
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
    # A tool runs only if its source checkbox is ticked -- by the visible depth preset or
    # by hand in Advanced options -- so the submitted tool list is read from the boxes.
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


# --- SpiderFoot NG (HTTP tool) -------------------------------------------------


def _sf_settings(**kw):
    base = {
        "spiderfoot_url": "http://spiderfoot.invalid",
        "spiderfoot_api_key": "sf_test_key",
        "spiderfoot_modules": ("sfp_dnsresolve",),
    }
    base.update(kw)
    return Settings(tools=frozenset({"spiderfoot"}), **base)


class _FakeSF:
    """Minimal stand-in for the SpiderFoot REST API, exercising the real contract:
    create returns an id, status must reach FINISHED, events paginate."""

    def __init__(self, statuses, pages, create_status=201):
        self.statuses = list(statuses)
        self.pages = list(pages)
        self.create_status = create_status
        self.headers_seen = []
        self.deleted = []
        self.created = []

    def handler(self, request):
        import httpx

        self.headers_seen.append(dict(request.headers))
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/scans":
            self.created.append(json.loads(request.content))
            if self.create_status != 201:
                return httpx.Response(self.create_status, json={"detail": "nope"})
            return httpx.Response(201, json={"id": "ABC123"})
        if request.method == "DELETE" or path.endswith("/stop"):
            self.deleted.append(request.method + " " + path)
            return httpx.Response(200, json={})
        if path.endswith("/events"):
            page = int(request.url.params.get("page", 1))
            events, has_next = self.pages[page - 1]
            return httpx.Response(200, json={"events": events, "has_next": has_next})
        return httpx.Response(200, json={"status": self.statuses.pop(0)})


@pytest.fixture
def fake_sf(monkeypatch):
    import httpx

    created = {}

    def install(fake):
        real_client = httpx.Client

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(fake.handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", patched)
        created["fake"] = fake
        return fake

    return install


def test_spiderfoot_scan_returns_events_as_records(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(
        _FakeSF(
            statuses=["RUNNING", "FINISHED"],
            pages=[
                ([{"type": "IP_ADDRESS", "data": "1.2.3.4"}], True),
                ([{"type": "DOMAIN_NAME", "data": "example.com"}], False),
            ],
        )
    )
    capture = tools.run("spiderfoot", "example.com", _sf_settings())
    body = json.loads(capture.body)
    assert body["tool"] == "spiderfoot" and body["target"] == "example.com"
    # Both pages are collected, so a multi-page scan is not silently truncated. The
    # DOMAIN_NAME event on page 2 is the scan's own target echoed back, so it is dropped:
    # restating the input as a finding is what made these captures read as noise.
    assert [r["data"] for r in body["results"]] == ["1.2.3.4"]
    assert fake.created[0]["modules"] == ["sfp_dnsresolve"]
    assert fake.created[0]["target"] == "example.com"
    # An explicit target_type is always sent so the fork does not have to infer it from the
    # string (which is what made a bare username unscannable).
    assert fake.created[0]["target_type"] == "INTERNET_NAME"


def test_spiderfoot_target_type_is_explicit_per_shape():
    assert tools._spiderfoot_target_type("me@x.test") == "EMAILADDR"
    assert tools._spiderfoot_target_type("example.com") == "INTERNET_NAME"
    # The case the old denial refused: a bare handle is now a scannable USERNAME.
    assert tools._spiderfoot_target_type("Snaxxwax") == "USERNAME"


def test_spiderfoot_separates_existence_from_ownership():
    """Existence and ownership are different questions, reported separately.

    SpiderFoot reports observations and inferences both at confidence 100, so without the
    existence ceiling an inferred HUMAN_NAME ties with a profile that was actually read. And
    an account reached by a USERNAME derived from an address's local part exists, but a
    matching handle is NOT proof the target owns it -- so ownership is never "confirmed".
    """
    events = [
        # A real account, but reached via a USERNAME derived from the email's local part.
        {"type": "ROOT", "data": "me@x.test", "hash": "R", "source_event_hash": "R"},
        {
            "type": "USERNAME",
            "data": "me",
            "hash": "U",
            "source_event_hash": "R",
            "module": "sfp_accounts",
        },
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "https://x.test/a",
            "hash": "A",
            "source_event_hash": "U",
            "module": "sfp_accounts",
        },
        {"type": "SIMILAR_ACCOUNT_EXTERNAL", "data": "https://x.test/a2", "module": "sfp_accounts"},
        {"type": "AFFILIATE_EMAILADDR", "data": "other@x.test", "module": "sfp_pgp"},
        {"type": "HUMAN_NAME", "data": "Guessed Name", "module": "sfp_names"},
    ]
    by_type = {r["event_type"]: r for r in tools._spiderfoot_records(events, "me@x.test")}

    acct = by_type["ACCOUNT_EXTERNAL_OWNED"]
    # The profile was fetched, so it was OBSERVED -- but it was reached by a derived handle,
    # so ownership is only a candidate, never confirmed.
    assert acct["existence"] == "observed"
    assert acct["ownership"] == "candidate"
    assert acct["derived_via"] == "USERNAME"
    assert acct["_confidence"] > 0.5

    for inferred in ("SIMILAR_ACCOUNT_EXTERNAL", "AFFILIATE_EMAILADDR", "HUMAN_NAME"):
        assert by_type[inferred]["existence"] == "inferred", inferred
        assert by_type[inferred]["ownership"] == "candidate", inferred
        # The ceiling keeps a guess from outranking an observation.
        assert by_type[inferred]["_confidence"] <= 0.5, inferred

    # No record from a tool ever asserts ownership.
    assert all(r["ownership"] != "confirmed" for r in by_type.values())


def test_spiderfoot_extracts_sfurl_links_and_records_provenance():
    """Real sfp_accounts output: a label, then the URL inside an <SFURL> tag.

    Shaped from the actual rows a scan of an owned address produced. Treating the whole
    string as the value cost two things at once -- `http_url()` rejected it so the account
    produced NO follow-up lead, and with no `url` the record fell back to a content
    fingerprint, minting a new entity whenever the label changed.

    `derived_via` is the other half: for an EMAIL target sfp_accounts derives a USERNAME from
    the local part and hangs the accounts off THAT, so an account reported for an address was
    reached by handle. The chain is the only thing that says so.
    """
    events = [
        {"type": "ROOT", "data": "me@x.test", "hash": "ROOT", "source_event_hash": "ROOT"},
        {
            "type": "EMAILADDR",
            "data": "me@x.test",
            "module": "SpiderFoot UI",
            "hash": "H_E",
            "source_event_hash": "ROOT",
        },
        {
            "type": "USERNAME",
            "data": "me",
            "module": "sfp_accounts",
            "hash": "H_U",
            "source_event_hash": "H_E",
        },
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "Pinterest (Category: social)\n<SFURL>https://www.pinterest.com/me/</SFURL>",
            "module": "sfp_accounts",
            "hash": "H_P",
            "source_event_hash": "H_U",
        },
    ]
    by_type = {r["event_type"]: r for r in tools._spiderfoot_records(events, "me@x.test")}

    account = by_type["ACCOUNT_EXTERNAL_OWNED"]
    # The URL is extracted, so it becomes a lead and a stable entity key...
    assert account["url"] == "https://www.pinterest.com/me/"
    # ...and the label survives as the queryable value, without the tag soup.
    assert account["data"] == "Pinterest (Category: social)"
    assert "SFURL" not in account["data"]
    # Provenance: reached via a handle, not via anything tying the address to the profile.
    assert account["derived_via"] == "USERNAME"
    assert by_type["USERNAME"]["derived_via"] == "EMAILADDR"
    # And the parent's VALUE, which is the only field on an account a dossier can match an
    # identifier against -- its own `data` is a label. Without it the accounts land in the
    # dossier's `unresolved` list: found, evidenced, and attached to nobody.
    assert account["derived_from"] == "me"
    assert by_type["USERNAME"]["derived_from"] == "me@x.test"


def test_spiderfoot_dedupes_the_same_profile_under_two_labels():
    """One profile reported with different labels is one account, keyed on its URL."""
    events = [
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "Pinterest (Category: social)\n<SFURL>https://pin.test/me</SFURL>",
            "module": "sfp_accounts",
            "confidence": 80,
        },
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "Pinterest\n<SFURL>https://pin.test/me</SFURL>",
            "module": "sfp_social",
            "confidence": 100,
        },
    ]
    records = tools._spiderfoot_records(events, "me@x.test")
    assert len(records) == 1
    assert records[0]["related_modules"] == ["sfp_accounts"]


def test_spiderfoot_dedupes_one_fact_found_by_several_modules():
    """Same (type, data) from N modules is one record naming the others, not N records."""
    events = [
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "https://github.test/u",
            "module": "sfp_github",
            "confidence": 80,
        },
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "https://github.test/u",
            "module": "sfp_accounts",
            "confidence": 100,
        },
        {
            "type": "ACCOUNT_EXTERNAL_OWNED",
            "data": "https://github.test/u",
            "module": "sfp_social",
            "confidence": 50,
        },
    ]
    records = tools._spiderfoot_records(events, "me@x.test")

    assert len(records) == 1
    kept = records[0]
    # The strongest survives...
    assert kept["module"] == "sfp_accounts" and kept["_confidence"] == 1.0
    # ...and the evidence that the others saw it too is not discarded.
    assert kept["related_modules"] == ["sfp_github", "sfp_social"]
    # A URL-valued event carries `url`, which is both the lead and the stable entity key.
    assert kept["url"] == "https://github.test/u"


def test_spiderfoot_drops_its_own_input_and_empty_events():
    """ROOT, the echoed target, and data-less events are not findings."""
    events = [
        {"type": "ROOT", "data": "me@x.test", "module": ""},
        {"type": "EMAILADDR", "data": "ME@X.TEST", "module": "sfp_x"},  # echo, any case
        {"type": "PUBLIC_CODE_REPO", "data": "", "module": "sfp_github"},
        {"type": "PUBLIC_CODE_REPO", "data": None, "module": "sfp_github"},
        {"type": "", "data": "orphan", "module": "sfp_github"},
        {"type": "GEOINFO", "data": "Berlin", "module": "sfp_gravatar"},
    ]
    records = tools._spiderfoot_records(events, "me@x.test")
    assert [r["event_type"] for r in records] == ["GEOINFO"]


def test_spiderfoot_egress_declaration_is_fail_closed():
    """proxy-only mode refuses the tool unless the deployment declares a proxied scanner.

    The old check read SpiderFoot's own `_socks*` config, which is never reloaded at startup
    and which its scanner ignores -- so it both always failed AND would have proved nothing.
    """
    from harvest.models import PolicyDenied

    direct = _sf_settings(egress_mode="proxy", proxy="http://relay:8888")
    with pytest.raises(PolicyDenied, match="HARVEST_SPIDERFOOT_EGRESS"):
        tools._assert_spiderfoot_proxied(direct)

    declared = _sf_settings(
        egress_mode="proxy", proxy="http://relay:8888", spiderfoot_egress="proxy-env"
    )
    tools._assert_spiderfoot_proxied(declared)  # does not raise


def test_spiderfoot_sends_api_key_and_never_logs_it(fake_sf, monkeypatch, caplog):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(_FakeSF(statuses=["FINISHED"], pages=[([], False)]))
    with caplog.at_level(logging.DEBUG, logger="harvest"):
        tools.run("spiderfoot", "example.com", _sf_settings())
    assert fake.headers_seen[0]["x-api-key"] == "sf_test_key"
    assert "sf_test_key" not in caplog.text


def test_spiderfoot_rejected_key_is_policy_denied(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake_sf(_FakeSF(statuses=[], pages=[], create_status=401))
    with pytest.raises(PolicyDenied, match="HARVEST_SPIDERFOOT_API_KEY"):
        tools.run("spiderfoot", "example.com", _sf_settings())


@pytest.mark.parametrize(
    "missing,match",
    [
        ({"spiderfoot_url": ""}, "HARVEST_SPIDERFOOT_URL"),
        ({"spiderfoot_api_key": ""}, "HARVEST_SPIDERFOOT_API_KEY"),
        ({"spiderfoot_modules": ()}, "HARVEST_SPIDERFOOT_MODULES"),
    ],
)
def test_spiderfoot_requires_configuration_before_any_request(fake_sf, missing, match):
    fake = fake_sf(_FakeSF(statuses=["FINISHED"], pages=[([], False)]))
    with pytest.raises(PolicyDenied, match=match):
        tools.run("spiderfoot", "example.com", _sf_settings(**missing))
    assert not fake.created


def test_spiderfoot_failed_scan_is_an_error_not_a_partial_capture(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake_sf(_FakeSF(statuses=["RUNNING", "ERROR-FAILED"], pages=[([], False)]))
    with pytest.raises(ValueError, match="ERROR-FAILED"):
        tools.run("spiderfoot", "example.com", _sf_settings())


def test_spiderfoot_cancellation_stops_the_scan(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(_FakeSF(statuses=["RUNNING"] * 5, pages=[([], False)]))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(LostLease):
        tools.run("spiderfoot", "example.com", _sf_settings(), cancelled=cancelled)
    assert fake.deleted == ["POST /api/v1/scans/ABC123/stop"]


def test_spiderfoot_timeout_stops_the_scan_instead_of_orphaning_it(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(_FakeSF(statuses=["RUNNING"] * 50, pages=[([], False)]))
    settings = _sf_settings()
    settings.tool_timeout = 0.0
    # Out of time keeps the evidence: the scan is stopped once, its events so far are kept
    # and the capture says it is partial (tests/test_v1_crawl_quality.py covers the order).
    body = json.loads(tools.run("spiderfoot", "example.com", settings).body)
    assert "stopped after" in body["partial"]
    assert fake.deleted == ["POST /api/v1/scans/ABC123/stop"]


def test_spiderfoot_finished_scan_is_not_stopped(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(_FakeSF(statuses=["FINISHED"], pages=[([], False)]))
    tools.run("spiderfoot", "example.com", _sf_settings())
    assert fake.deleted == []


# ---------------------------------------------------------------------------
# Proxy-only egress (HARVEST_EGRESS_MODE=proxy)
# ---------------------------------------------------------------------------
# The point of these: in proxy-only mode no path may quietly fall back to
# direct egress. Each test pins one path that previously could.


def test_proxy_mode_requires_a_proxy_url():
    import pytest

    from harvest.config import Settings

    with pytest.raises(ValueError, match="requires HARVEST_EGRESS_PROXY"):
        Settings(egress_mode="proxy", proxy=None)


def test_egress_mode_rejects_unknown_values():
    import pytest

    from harvest.config import Settings

    with pytest.raises(ValueError, match="must be 'direct' or 'proxy'"):
        Settings(egress_mode="sometimes")


def test_direct_mode_is_the_default_and_not_proxy_only():
    from harvest.config import Settings

    assert Settings().egress_mode == "direct"
    assert Settings().proxy_only is False


def test_maigret_refuses_to_run_when_direct_egress_still_works(monkeypatch, tmp_path):
    """The core guarantee: an open host must not be treated as proxy-only."""
    import pytest

    from harvest import tools
    from harvest.config import Settings
    from harvest.models import PolicyDenied

    settings = Settings(
        egress_mode="proxy", proxy="socks5://127.0.0.1:1080", egress_probe="1.1.1.1:443"
    )

    class OpenSocket:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    # Direct egress succeeds => proxy-only mode is not actually enforced.
    monkeypatch.setattr(tools.socket, "create_connection", lambda *a, **k: OpenSocket())
    ran = []
    monkeypatch.setattr(tools, "_exec", lambda *a, **k: ran.append(a))

    with pytest.raises(PolicyDenied, match="direct egress"):
        tools._maigret("someuser", str(tmp_path), 30.0, None, settings)
    assert ran == [], "maigret must not run at all when egress is not locked down"


def test_maigret_runs_when_direct_egress_is_blocked(monkeypatch, tmp_path):
    import json as _json

    from harvest import tools
    from harvest.config import Settings

    settings = Settings(egress_mode="proxy", proxy="socks5://127.0.0.1:1080")

    def blocked(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(tools.socket, "create_connection", blocked)

    captured = {}

    def fake_exec(argv, *a, **k):
        captured["argv"] = argv
        (tmp_path / "report_someuser_simple.json").write_text(
            _json.dumps({"GitHub": {"url_user": "https://github.com/someuser"}})
        )

    monkeypatch.setattr(tools, "_exec", fake_exec)
    out = tools._maigret("someuser", str(tmp_path), 30.0, None, settings)

    argv = captured["argv"]
    assert "--proxy" in argv and argv[argv.index("--proxy") + 1] == "socks5://127.0.0.1:1080"
    assert "--no-autoupdate" in argv, "the site-DB update ignores --proxy"
    assert out[0]["url"] == "https://github.com/someuser"


def test_cloudflare_bypass_is_refused_in_proxy_mode(monkeypatch, tmp_path):
    """FlareSolverr fetches from its own container, so --proxy never covers it."""
    import pytest

    from harvest import tools
    from harvest.config import Settings
    from harvest.models import PolicyDenied

    settings = Settings(
        egress_mode="proxy",
        proxy="socks5://127.0.0.1:1080",
        maigret_cloudflare_bypass=True,
    )
    monkeypatch.setattr(tools, "_exec", lambda *a, **k: None)
    with pytest.raises(PolicyDenied, match="CLOUDFLARE_BYPASS"):
        tools._maigret("someuser", str(tmp_path), 30.0, None, settings)


def _proxy_sf_settings(**kw):
    from harvest.config import Settings

    return Settings(
        egress_mode="proxy",
        proxy="socks5://10.0.0.9:1080",
        spiderfoot_url="http://sf-api:8001",
        spiderfoot_api_key="sf_test",
        spiderfoot_modules=("sfp_dnsresolve",),
        **kw,
    )


def test_spiderfoot_is_refused_unless_its_container_egress_is_declared():
    """SpiderFoot's modules run in their own container, so this process cannot observe them.

    This replaces five tests that asserted against SpiderFoot's own `_socks*` config. That
    check was removed because it was measured to be both always-failing and meaningless on
    SpiderFoot NG 6.1.0: `GET /api/v1/config` answers from per-uvicorn-worker memory that is
    never reloaded from the database (so the value read back was "" or nondeterministic), and
    the scanner ignores the setting regardless -- a scan whose modules reached their targets
    moved 0 bytes through the relay. Asserting it gated the tool on a value with no bearing
    on where the packets went.

    What is asserted now is the operator's declaration, and it is fail-closed: the default
    refuses. verify-deployment.sh checks the container-level truth it stands for.
    """
    with pytest.raises(PolicyDenied, match="HARVEST_SPIDERFOOT_EGRESS"):
        tools._assert_spiderfoot_proxied(_proxy_sf_settings())


def test_spiderfoot_runs_when_container_egress_is_declared_proxied():
    tools._assert_spiderfoot_proxied(_proxy_sf_settings(spiderfoot_egress="proxy-env"))


def test_spiderfoot_egress_declaration_defaults_to_direct():
    """Nobody gets proxied egress by forgetting to configure it."""
    from harvest.config import Settings

    assert Settings().spiderfoot_egress == "direct"


def test_spiderfoot_tool_is_blocked_end_to_end_without_the_declaration(fake_sf, monkeypatch):
    """The denial must happen before any scan is created, not after."""
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(_FakeSF(statuses=["FINISHED"], pages=[([], False)]))
    enabled_sf = _proxy_sf_settings(tools=frozenset({"spiderfoot"}))
    with pytest.raises(PolicyDenied, match="HARVEST_SPIDERFOOT_EGRESS"):
        tools.run("spiderfoot", "example.com", enabled_sf)
    assert fake.created == []


# Shaped after GHunt 2.3.4's own writer (modules/email.py) and parsers (parsers/people.py):
# every sub-object is keyed by the Google "container" name, two to three levels down.
GHUNT_REPORT = {
    "PROFILE_CONTAINER": {
        "profile": {
            "personId": "109274296986499857531",
            "names": {"PROFILE": {"fullname": "Jane Doe", "firstName": "Jane", "lastName": "Doe"}},
            "emails": {"PROFILE": {"value": "jane.doe@gmail.com"}},
            "profilePhotos": {
                "PROFILE": {"url": "https://lh3.googleusercontent.com/a/x", "isDefault": False}
            },
            "profileInfos": {"PROFILE": {"userTypes": ["GOOGLE_USER", "GPLUS_USER"]}},
        },
        "maps": {"reviews": []},
    }
}


@pytest.fixture
def fake_ghunt(monkeypatch, tmp_path):
    """Stand in for the binary but keep the real contract: ghunt writes the --json path
    and prints nothing the adapter reads. HOME is redirected so the creds precheck has
    something to find without touching the developer's own ~/.malfrats."""
    (tmp_path / ".malfrats" / "ghunt").mkdir(parents=True)
    (tmp_path / ".malfrats" / "ghunt" / "creds.m").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = _Created()
    calls.envs = []

    def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
        calls.append(argv)
        calls.envs.append(env)
        report = argv[argv.index("--json") + 1]
        with open(report, "w", encoding="utf-8") as handle:
            json.dump(GHUNT_REPORT, handle)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(tools, "_exec", fake_exec)
    return calls


def ghunt_enabled(**kwargs):
    return Settings(tools=frozenset({"ghunt"}), **kwargs)


def test_ghunt_yields_one_entity_keyed_by_gaia_id(fake_ghunt):
    capture = tools.run("ghunt", "jane.doe@gmail.com", ghunt_enabled())
    assert capture.url == "tool://ghunt/jane.doe@gmail.com"
    body = json.loads(capture.body)
    assert body["tool"] == "ghunt" and body["target"] == "jane.doe@gmail.com"
    assert len(body["results"]) == 1
    record = body["results"][0]
    # `id` is what keeps record_key off a content fingerprint, so the account survives
    # a rerun as one entity instead of forking on any changed profile detail.
    assert record["id"] == "109274296986499857531"
    assert record["email"] == "jane.doe@gmail.com"
    assert record["container"] == "PROFILE_CONTAINER"
    argv = fake_ghunt[0]
    assert argv[:3] == ["ghunt", "email", "jane.doe@gmail.com"]


def test_ghunt_profile_fields_are_promoted_out_of_the_nested_container(fake_ghunt):
    """GHunt buries every useful value under profile.<section>.PROFILE.<field>, which no
    dossier field_map can address -- the same defect maigret's status.ids had."""
    record = json.loads(tools.run("ghunt", "jane.doe@gmail.com", ghunt_enabled()).body)["results"][
        0
    ]
    assert record["fullname"] == "Jane Doe"
    assert record["first_name"] == "Jane"
    assert record["last_name"] == "Doe"
    assert record["email_profile"] == "jane.doe@gmail.com"
    assert record["image_url"] == "https://lh3.googleusercontent.com/a/x"
    assert record["profile_photo_is_default"] is False
    assert record["user_types"] == ["GOOGLE_USER", "GPLUS_USER"]
    # The Gaia ID still identifies the entity, not a promoted field.
    assert record["id"] == "109274296986499857531"
    # Nothing is discarded: the nested container remains as evidence.
    assert record["profile"]["names"]["PROFILE"]["fullname"] == "Jane Doe"


def test_ghunt_promotion_degrades_to_absent_rather_than_wrong(fake_ghunt, monkeypatch):
    """A schema change must lose a field, never invent one of the wrong type."""
    assert tools._ghunt_profile({}) == {}
    assert tools._ghunt_profile({"names": "not-a-dict"}) == {}
    assert tools._ghunt_profile({"names": {"PROFILE": {"fullname": ""}}}) == {}
    # bool is an int subclass; an exact type check keeps it out of a string field.
    assert tools._ghunt_profile({"names": {"PROFILE": {"fullname": True}}}) == {}
    assert tools._ghunt_profile({"profilePhotos": {"PROFILE": {"isDefault": 1}}}) == {}, (
        "1 is not a boolean"
    )
    # A legitimate False must still be promoted: "this is not the default avatar" is a fact.
    assert tools._ghunt_profile({"profilePhotos": {"PROFILE": {"isDefault": False}}}) == {
        "profile_photo_is_default": False
    }


def test_ghunt_refuses_without_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(PolicyDenied, match="no credentials"):
        tools.run("ghunt", "jane.doe@gmail.com", ghunt_enabled())


def test_ghunt_receives_the_proxy_through_the_environment(fake_ghunt):
    # GHunt has no --proxy flag, so the env IS the route; a dropped variable would mean
    # silent direct egress rather than a visible failure.
    capture = tools.run("ghunt", "jane.doe@gmail.com", ghunt_enabled(proxy="http://relay:8888"))
    assert capture.status == 200
    env = fake_ghunt.envs[0]
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://relay:8888"
    assert "--proxy" not in fake_ghunt[0]


def test_ghunt_in_proxy_only_mode_refuses_when_direct_egress_works(fake_ghunt, monkeypatch):
    monkeypatch.setattr(
        tools,
        "_assert_no_direct_egress",
        lambda s: (_ for _ in ()).throw(PolicyDenied("direct egress succeeded")),
    )
    settings = ghunt_enabled(proxy="http://relay:8888", egress_mode="proxy")
    with pytest.raises(PolicyDenied, match="direct egress"):
        tools.run("ghunt", "jane.doe@gmail.com", settings)
    assert not fake_ghunt  # refused before launch, not after a leaked request


def test_email_plan_suggests_ghunt():
    plan = plan_investigation("jane.doe@gmail.com")
    assert plan.kind == "email"
    assert {"name": "ghunt", "target": "jane.doe@gmail.com"} in plan.tools


# -- Maigret result quality ------------------------------------------------
#
# These run against tests/data/maigret_snaxxwax_simple.json: six sites trimmed out of a real
# saved `--all-sites` scan (capture 212 on the ovh-vps deployment) for the handle Snaxxwax.
# Real output rather than a hand-written shape, because every defect below was something the
# hand-written fixture above was too clean to show: an error page reported as a claimed
# account, probe sentinels promoted to evidence, unexpanded {username} templates reaching the
# crawler, and one forum counted twice because it answers on two hosts.

SNAXXWAX_REPORT = json.loads(
    (Path(__file__).parent / "data" / "maigret_snaxxwax_simple.json").read_text(encoding="utf-8")
)


@pytest.fixture
def saved_snaxxwax_scan(monkeypatch):
    """Replay the saved scan instead of spending another ~45 MiB of proxy allowance."""

    def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
        workdir = argv[argv.index("--folderoutput") + 1]
        report = Path(workdir) / f"report_{argv[1]}_simple.json"
        report.write_text(json.dumps(SNAXXWAX_REPORT), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(tools, "_exec", fake_exec)


def _snaxxwax_records():
    capture = tools.run("maigret", "Snaxxwax", Settings(tools=frozenset({"maigret"})))
    return {r["sitename"]: r for r in json.loads(capture.body)["results"]}


def test_claimed_verdicts_are_graded_not_all_certain(saved_snaxxwax_scan):
    """Maigret says "Claimed" for very different evidence; the claims must not."""
    records = _snaxxwax_records()
    # GitHub's checker parsed real profile data off the response.
    assert records["GitHub"]["_confidence"] == 0.9
    # Amperka answered 403 -- the "presence" string it matched is on a login wall, not a
    # profile. This is the error page that was reported as a claimed account.
    assert records["Amperka"]["http_status"] == 403
    assert records["Amperka"]["_confidence"] == 0.3
    # heroesworld.ru is a bare status-code check with nothing parsed: weak, but not an error.
    assert records["heroesworld.ru"]["_confidence"] == 0.4


def test_profile_ids_become_queryable_top_level_fields(saved_snaxxwax_scan):
    """status.ids held the useful evidence as one opaque blob; it is flat fields now."""
    github = _snaxxwax_records()["GitHub"]
    assert github["uid"] == "105263527"
    assert github["created_at"] == "2022-05-10T04:32:39Z"
    assert github["status"] == "Claimed"  # the verdict, not the nested object
    assert "ids" not in github
    # The Threads hit is an unverified handle match -- only github.com/Snaxxwax is ground
    # truth here -- so the real display name it reported is redacted in the fixture. The
    # point is that a name arrives as its own sourced field, not that it is correct.
    assert _snaxxwax_records()["Threads"]["fullname"] == "Redacted Name"
    # The extractor's own metadata is not an observation about the subject.
    assert "_extractor" not in github


def test_site_definition_sentinels_and_templates_never_become_evidence(saved_snaxxwax_scan):
    """usernameClaimed/usernameUnclaimed are probe fixtures and {username} is a template."""
    body = json.dumps(_snaxxwax_records())
    assert "noonewouldeverusethis7" not in body
    assert "usernameClaimed" not in body
    assert "{username}" not in body
    assert "regexCheck" not in body
    # The site's own front page is navigation, not profile enrichment.
    records = _snaxxwax_records()
    assert "url_main" not in records["GitHub"]
    # maigret's "unranked" is sys.maxsize, which as a claim reads as a real 19-digit rank.
    assert records["GitHub"]["rank"] == 10
    assert "rank" not in records["Amperka"]


def test_one_forum_on_two_hosts_is_one_account(saved_snaxxwax_scan):
    """antichat.io and forum.antichat.io share a vBulletin definition and one account."""
    records = _snaxxwax_records()
    assert "forum.antichat.io" not in records
    kept = records["antichat.io"]
    # Nothing is discarded: the merged host stays on the record as evidence.
    assert kept["related_sites"] == [
        "forum.antichat.io: https://forum.antichat.io/member.php?username=Snaxxwax"
    ]
    # A shared path alone must never merge two unrelated domains.
    assert "heroesworld.ru" in records and "GitHub" in records


def test_only_profiles_are_offered_as_crawl_leads(saved_snaxxwax_scan):
    """The leads a saved scan produces, which is what follow-up crawling would fetch."""
    from harvest.extract import JsonAdapter

    capture = tools.run("maigret", "Snaxxwax", Settings(tools=frozenset({"maigret"})))
    leads = {lead.url for lead in JsonAdapter().extract(capture.body, capture.url).leads}
    assert "https://github.com/Snaxxwax" in leads  # the profile
    assert "https://api.github.com/users/Snaxxwax" in leads  # its enrichment endpoint
    # Front pages, unexpanded templates and avatars are all wasted fetches.
    assert not any(u in leads for u in ("https://www.github.com/", "https://github.com/"))
    assert not any("{" in u for u in leads)
    assert not any("avatars.githubusercontent.com" in u for u in leads)


def test_top_sites_trades_breadth_for_a_smaller_scan(saved_snaxxwax_scan, monkeypatch):
    argv = []
    monkeypatch.setattr(tools, "_exec", lambda a, *r, **k: argv.append(a) or _write(a))
    settings = Settings(tools=frozenset({"maigret"}))
    tools.run("maigret", "Snaxxwax", settings, top_sites=100)
    assert "--top-sites" in argv[0] and argv[0][argv[0].index("--top-sites") + 1] == "100"
    assert "--all-sites" not in argv[0]
    argv.clear()
    tools.run("maigret", "Snaxxwax", settings)
    assert "--all-sites" in argv[0] and "--top-sites" not in argv[0]
    # Breadth never relaxes the two unconditional flags.
    assert "--no-autoupdate" in argv[0]


def _write(argv):
    report = Path(argv[argv.index("--folderoutput") + 1]) / f"report_{argv[1]}_simple.json"
    report.write_text(json.dumps(SNAXXWAX_REPORT), encoding="utf-8")


def test_an_unexpanded_template_is_not_a_valid_url():
    from harvest.models import canonical_url

    with pytest.raises(ValueError, match="template placeholder"):
        canonical_url("https://t.me/{username}")


# --- Per-input SpiderFoot module selection (the scan sends a plan, not the allowlist). ---

_DEPLOYED_SF = (
    "sfp_dnsresolve,sfp_accounts,sfp_tiktok_osint,sfp_gravatar,sfp_hudsonrock,"
    "sfp_pgp,sfp_debounce,sfp_names,sfp_wikileaks"
).split(",")


def test_spiderfoot_plan_differs_by_input_type():
    s = _sf_settings(spiderfoot_modules=tuple(_DEPLOYED_SF))
    email = tools.spiderfoot_plan("EMAILADDR", s)["modules"]
    user = tools.spiderfoot_plan("USERNAME", s)["modules"]
    host = tools.spiderfoot_plan("INTERNET_NAME", s)
    assert set(email) == set(_DEPLOYED_SF) - {"sfp_dnsresolve"}
    assert user == ["sfp_accounts", "sfp_hudsonrock", "sfp_tiktok_osint"]
    # Host target: infrastructure modules only. The rest would be fed sfp_pgp's addresses --
    # other people at the domain -- inside the scan, so they are left out with that reason.
    assert host["modules"] == ["sfp_dnsresolve", "sfp_pgp"]
    for module in ("sfp_accounts", "sfp_hudsonrock", "sfp_wikileaks"):
        assert "EMAILADDR from sfp_pgp" in host["excluded"][module], module
    assert "consumes nothing reachable" in host["excluded"]["sfp_gravatar"]
    # Without pgp there is no such hand-off, and DOMAIN_NAME (from dnsresolve) is what lets
    # the DOMAIN_NAME-only modules fire: dependency, not just direct input.
    no_pgp = tools.spiderfoot_plan(
        "INTERNET_NAME", _sf_settings(spiderfoot_modules=tuple(set(_DEPLOYED_SF) - {"sfp_pgp"}))
    )
    assert no_pgp["consumes"]["sfp_wikileaks"] == ["DOMAIN_NAME"]
    assert "sfp_accounts" in no_pgp["modules"]
    # Non-unique names still pass between person modules: disclosed, not hidden.
    assert {"from": "sfp_names", "event": "HUMAN_NAME", "to": "sfp_accounts"} in (
        tools.spiderfoot_plan("EMAILADDR", s)["cascades"]
    )


def test_spiderfoot_plan_gates_keyed_modules_on_declared_credentials():
    s = _sf_settings(spiderfoot_modules=("sfp_accounts", "sfp_c99"))
    plan = tools.spiderfoot_plan("USERNAME", s)
    assert plan["modules"] == ["sfp_accounts"]
    assert "API key" in plan["excluded"]["sfp_c99"]
    keyed = _sf_settings(
        spiderfoot_modules=("sfp_accounts", "sfp_c99"),
        spiderfoot_keyed_modules=frozenset({"sfp_c99"}),
    )
    assert tools.spiderfoot_plan("USERNAME", keyed)["modules"] == ["sfp_accounts", "sfp_c99"]
    # Keyed, but observed producing events with no key configured: usable keyless.
    tiktok = tools.spiderfoot_plan(
        "USERNAME", _sf_settings(spiderfoot_modules=("sfp_tiktok_osint",))
    )
    assert tiktok["modules"] == ["sfp_tiktok_osint"]


def test_spiderfoot_plan_discloses_unknown_untested_and_not_enabled():
    plan = tools.spiderfoot_plan(
        "USERNAME", _sf_settings(spiderfoot_modules=("sfp_github", "sfp_nope"))
    )
    assert "unknown" in plan["excluded"]["sfp_nope"]
    assert plan["tested"] == {"sfp_github": "untested"}
    assert "sfp_keybase" in plan["not_enabled"]
    email = tools.spiderfoot_plan("EMAILADDR", _sf_settings(spiderfoot_modules=("sfp_accounts",)))
    assert email["tested"] == {"sfp_accounts": "events"}


def test_spiderfoot_scan_sends_the_input_plan_and_records_it(fake_sf, monkeypatch):
    monkeypatch.setattr(tools.time, "sleep", lambda _s: None)
    fake = fake_sf(_FakeSF(statuses=["FINISHED"], pages=[([], False)]))
    s = _sf_settings(spiderfoot_modules=tuple(_DEPLOYED_SF))
    capture = tools.run("spiderfoot", "Snaxxwax", s)
    assert fake.created[0]["modules"] == ["sfp_accounts", "sfp_hudsonrock", "sfp_tiktok_osint"]
    assert fake.created[0]["target_type"] == "USERNAME"
    body = json.loads(capture.body)
    assert body["modules"]["modules"] == fake.created[0]["modules"]
    assert "sfp_dnsresolve" in body["modules"]["excluded"]


def test_spiderfoot_refuses_a_scan_no_module_can_serve(fake_sf):
    fake = fake_sf(_FakeSF(statuses=["FINISHED"], pages=[([], False)]))
    with pytest.raises(PolicyDenied, match="USERNAME"):
        tools.run("spiderfoot", "Snaxxwax", _sf_settings())  # only sfp_dnsresolve allowlisted
    assert not fake.created


def test_capabilities_report_spiderfoot_per_input():
    from harvest import capabilities

    s = _sf_settings(spiderfoot_modules=("sfp_dnsresolve",), spiderfoot_egress="proxy-env")
    by_kind = {
        k: {c["name"]: c for c in capabilities.for_kind(k, s)} for k in ("domain", "username")
    }
    assert by_kind["domain"]["spiderfoot"]["ready"] is True
    assert by_kind["username"]["spiderfoot"]["ready"] is False
    assert "USERNAME" in by_kind["username"]["spiderfoot"]["detail"]
    plans = capabilities.readiness(s)["spiderfoot"]["plans"]
    assert plans["INTERNET_NAME"]["modules"] == ["sfp_dnsresolve"]
