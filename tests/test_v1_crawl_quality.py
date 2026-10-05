"""Regression tests for the defects observed in job 8986aa5c (captures 393-401).

Page bodies are trimmed, redacted copies of those captures (tests/fixtures/v1_crawl); the
maigret report mirrors capture 393's shape with the handle replaced. Everything is served
from the local test source, so no test here touches the network.
"""

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from harvest import tools
from harvest.config import Settings
from harvest.engine import Engine, auth_like, identifier_in_text, page_check
from harvest.models import ActionableError, JobSpec
from harvest.socid_adapter import SocidHtmlAdapter
from harvest.store import stop_advice

FIXTURES = Path(__file__).parent / "fixtures" / "v1_crawl"


def page(name):
    return (FIXTURES / name).read_text(), "text/html"


def report(base):
    return {
        "TikTok": {
            "url_user": f"{base}/tiktok/@exampleuser",
            "http_status": 200,
            "status": {
                "status": "Claimed",
                "ids": {"fullname": "Example Name", "tiktok_id": "7000000000000000001"},
            },
            "site": {"name": "TikTok", "checkType": "message"},
        },
        "Geocaching": {
            "url_user": f"{base}/p/?u=exampleuser",
            "http_status": 200,
            "status": {"status": "Claimed"},
            "site": {"name": "Geocaching", "checkType": "status_code"},
        },
        "Streaming": {
            "url_user": f"{base}/cb/exampleuser/",
            "http_status": 200,
            "status": {"status": "Claimed"},
            "site": {"name": "Streaming", "checkType": "status_code"},
        },
        "forum.archived": {
            "url_user": f"{base}/u/exampleuser",
            "url_probe": f"{base}/u/exampleuser.json",
            "http_status": 200,
            "status": {"status": "Claimed"},
            "site": {"name": "forum.archived", "checkType": "status_code"},
        },
    }


@pytest.fixture
def investigation(tmp_path, source, monkeypatch):
    base = source["base"]
    source["routes"] = {
        "/tiktok/@exampleuser": page("tiktok_profile.html"),
        "/p/": page("geocaching_profile.html"),
        "/account/signin/": page("geocaching_signin.html"),
        "/": page("chaturbate_home.html"),
        "/u/exampleuser": page("moonbeam_archived.html"),
        "/u/exampleuser.json": page("moonbeam_archived.html"),
        "/about/advertising.aspx": ("<html><title>Advertise</title></html>", "text/html"),
    }
    source["redirects"] = {"/cb/exampleuser/": f"{base}/?next=/cb/exampleuser/"}
    source["robots"] = "User-agent: *\nAllow: /\n"

    def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
        workdir = argv[argv.index("--folderoutput") + 1]
        Path(f"{workdir}/report_{argv[1]}_simple.json").write_text(json.dumps(report(base)))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(tools, "_exec", fake_exec)
    engine = Engine(
        Settings(
            database=str(tmp_path / "h.sqlite"),
            private_hosts=frozenset({"127.0.0.1"}),
            tools=frozenset({"maigret"}),
            proxy=None,
        )
    )
    spec = JobSpec.model_validate(
        {
            "objective": "Investigate exampleuser",
            "allowed_domains": ["127.0.0.1"],
            "fields": ["display_name", "username", "profile_url", "bio"],
            "tools": [{"name": "maigret", "target": "exampleuser"}],
            "limits": {"depth": 3, "domain_delay": 0.1, "requests": 100},
        }
    )
    job_id = engine.submit(spec)
    job = engine.run(job_id)
    return engine, job, source


def fetched(source):
    return [r.split("?")[0] for r in source["requests"] if not r.startswith("/robots.txt")]


def test_auth_pages_and_generic_page_links_are_not_crawled(investigation):
    engine, job, source = investigation
    paths = fetched(source)
    # The geocaching profile's "log in" link and everything behind it (OAuth, join) is gone.
    assert "/account/signin/" not in paths
    # The redirected-away homepage and the archived forum offer only site navigation.
    for nav in ("/privacy/", "/terms/", "/discover/", "/tags/", "/auth/login/"):
        assert nav not in paths
    # A real profile's ordinary links are still followed.
    assert "/about/advertising.aspx" in paths
    with engine.store.connection() as db:
        keys = [r[0] for r in db.execute("SELECT key FROM tasks WHERE job_id=?", (job["id"],))]
    assert not [k for k in keys if "signin" in k or "/auth/" in k]


def test_page_checks_separate_real_profiles_from_generic_pages(investigation):
    engine, job, _ = investigation
    summary = engine.store.job_summary(job["id"])
    checks = {a["site"]: a["page_check"] for a in summary["accounts"]}
    assert checks == {
        "TikTok": "identifier_present",
        "Geocaching": "identifier_present",
        "Streaming": "redirected_away",
        "forum.archived": "identifier_absent",
    }
    # The .json probe served the same bytes as the profile URL: a catch-all page.
    rows = [
        o for o in engine.store.observations(job["id"], limit=1000) if o["field"] == "page_check"
    ]
    assert {"duplicate_content"} <= {o["value"] for o in rows}
    # Existence, page evidence and ownership stay separate; nothing is owned.
    assert {a["ownership"] for a in summary["accounts"]} == {"candidate"}
    assert any("ownership" in u for u in summary["unknowns"])
    # Evidence for a present identifier sits exactly at its locator in the capture.
    present = next(o for o in rows if o["value"] == "identifier_present")
    capture = engine.store.capture(present["capture_ids"][0])
    start, end = map(int, present["locator"].removeprefix("chars:").split("-"))
    text = capture["body"].decode()
    assert text[start:end].lower() == "exampleuser" == present["evidence"].lower()


def test_requested_fields_map_to_tool_field_names(investigation):
    engine, job, _ = investigation
    assert "display_name" not in job["missing_fields"]
    assert "profile_url" not in job["missing_fields"]
    assert "bio" in job["missing_fields"]
    records = engine.store.job_records(job["id"])
    tiktok = next(r for r in records if r["entity_key"].endswith("/tiktok/@exampleuser"))
    assert tiktok["fields"]["display_name"]["value"] == "Example Name"
    assert tiktok["fields"]["display_name"]["via"] == "fullname"
    summary = engine.store.job_summary(job["id"])
    assert next(a for a in summary["accounts"] if a["site"] == "TikTok")["display_name"] == (
        "Example Name"
    )


def test_identifier_echoed_in_urls_is_not_presence():
    home = (FIXTURES / "chaturbate_home.html").read_text()
    signin = (FIXTURES / "geocaching_signin.html").read_text()
    assert identifier_in_text(home, "exampleuser") is None
    assert identifier_in_text(signin, "exampleuser") is None
    assert identifier_in_text((FIXTURES / "tiktok_profile.html").read_text(), "exampleuser")
    assert (
        page_check(
            ["exampleuser"],
            "https://x.test/account/signin/",
            "https://x.test/account/signin",
            signin,
        )[0]
        == "login_wall"
    )


@pytest.mark.parametrize(
    ("url", "text", "expected"),
    [
        ("https://x.test/account/signin/?returnUrl=%2Fp", "log in", True),
        ("https://x.test/account/oauth2/signinwithoauth2provider?provider=Apple", "", True),
        ("https://x.test/account/join?returnUrl=x", "Sign up", True),
        ("https://x.test/auth/login/", "", True),
        ("https://x.test/about/advertising.aspx", "Advertising with Us", False),
        ("https://x.test/joint-ventures", "Joint ventures", False),
        ("https://x.test/primary.json", "Primary register", False),  # a public register
        ("https://x.test/u/signinghero", "", True),  # ponytail ceiling: prefix heuristic
    ],
)
def test_auth_like(url, text, expected):
    assert auth_like(url, text) is expected


def test_ambiguous_socid_values_are_not_full_confidence(monkeypatch):
    html = '<html><p>22</p><script>{"followers":"22","id":"7000000000000000001"}</script></html>'
    monkeypatch.setattr(
        "harvest.socid_adapter.socid_extractor.extract",
        lambda page: {"_extractor": "X", "follower_count": "22", "uid": "7000000000000000001"},
    )
    claims = {
        c.field: c for c in SocidHtmlAdapter().extract(html.encode(), "https://x.test/u").claims
    }
    assert "ambiguous-value" in claims["follower_count"].locator
    assert claims["follower_count"].confidence == 0.5
    assert claims["uid"].locator.startswith("socid:X:chars:")
    assert claims["uid"].confidence == 1.0


@pytest.mark.parametrize(
    ("status", "reason", "fragment"),
    [
        ("budget_exhausted", "wall-clock deadline reached", "limits.seconds"),
        ("budget_exhausted", "investigation wall-clock deadline reached", "limits.seconds"),
        ("budget_exhausted", "investigation requests limit reached", "limits.requests"),
        ("budget_exhausted", "investigation tool_runs budget exhausted (3/3 used)", "tool_runs"),
        ("completed", None, None),
        (
            "partial",
            "frontier exhausted; a tool run hit its time allowance and kept",
            "limits.seconds",
        ),
    ],
)
def test_stop_advice_names_the_limit_that_stopped_the_job(status, reason, fragment):
    advice = stop_advice(status, reason)
    assert advice is None if fragment is None else fragment in advice


def _tool_engine(tmp_path, seconds, tool_timeout=600):
    engine = Engine(
        Settings(
            database=str(tmp_path / "h.sqlite"),
            tools=frozenset({"spiderfoot"}),
            tool_timeout=tool_timeout,
            proxy=None,
        )
    )
    spec = JobSpec.model_validate(
        {
            "objective": "scan",
            "tools": [{"name": "spiderfoot", "target": "exampleuser"}],
            "limits": {"seconds": seconds},
        }
    )
    return engine, engine.submit(spec)


def test_tool_run_is_bounded_by_the_remaining_wall_clock(tmp_path, monkeypatch):
    engine, job_id = _tool_engine(tmp_path, seconds=60)
    seen = {}

    def fake_run(name, target, settings, cancelled=None, top_sites=None, timeout=None):
        seen["timeout"] = timeout
        raise ActionableError("spiderfoot exceeded its 60s time allowance")

    monkeypatch.setattr("harvest.engine.run_tool", fake_run)
    engine.step(job_id)
    # Ends 15s before the 60s deadline, leaving time to read results and save the capture.
    assert 40 <= seen["timeout"] <= 45
    events = engine.store.events(job_id)
    assert any(e["type"] == "tool_started" and e["details"]["max_seconds"] <= 45 for e in events)
    failed = next(e for e in events if e["type"] == "failed")
    # The actual reason, not "ValueError: invalid source or adapter result".
    assert failed["details"]["reason"] == "spiderfoot exceeded its 60s time allowance"


def test_spiderfoot_timeout_keeps_partial_events_and_stops_the_scan(monkeypatch):
    import harvest.tools as t

    calls = []

    def handler(request):
        calls.append(request.method + " " + request.url.path)
        if request.method == "POST" and request.url.path == "/api/v1/scans":
            return httpx.Response(201, json={"id": "S1"})
        if request.url.path.endswith("/events"):
            events = [
                {
                    "type": "ACCOUNT_EXTERNAL_OWNED",
                    "data": "https://x.test/a",
                    "module": "sfp_accounts",
                }
            ]
            return httpx.Response(200, json={"events": events, "has_next": False})
        return httpx.Response(200, json={"status": "RUNNING"})

    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    settings = Settings(
        tools=frozenset({"spiderfoot"}),
        spiderfoot_url="http://sf.test",
        spiderfoot_api_key="k",
        spiderfoot_modules=("sfp_accounts",),
        tool_timeout=600,
        proxy=None,
    )
    body = json.loads(t.run("spiderfoot", "exampleuser", settings, timeout=0).body)
    assert "partial" in body and "stopped" in body["partial"]
    assert [r["data"] for r in body["results"]] == ["https://x.test/a"]
    stop = calls.index("POST /api/v1/scans/S1/stop")
    # Stopped first (no more external requests), then the events so far are read.
    assert stop < calls.index("GET /api/v1/scans/S1/events")
    assert calls.count("POST /api/v1/scans/S1/stop") == 1


def test_redirect_to_an_invalid_url_is_a_policy_block(engine, source):
    source["redirects"] = {"/apple": "https://appleid.test/auth?scope=name email"}
    spec = JobSpec.model_validate(
        {
            "objective": "redirect check",
            "seeds": [source["base"] + "/apple"],
            "limits": {"domain_delay": 0.1},
        }
    )
    job = engine.run(engine.submit(spec))
    blocked = [e for e in engine.store.events(job["id"]) if e["type"] == "blocked"]
    assert blocked and blocked[0]["details"]["reason"] == "redirect to a non-HTTP(S) or invalid URL"


def test_spiderfoot_scan_that_never_starts_fails_fast_and_is_stopped(monkeypatch):
    """Job 8986aa5c: the celery child failed the scan in 0.1s (Postgres pool exhausted), the
    status stayed non-terminal with started=0, and Harvest polled it for 600s."""
    import harvest.tools as t

    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(t.time, "time", lambda: next(clock))
    monkeypatch.setattr(t.time, "sleep", lambda _s: None)
    calls = []

    def handler(request):
        calls.append(request.method + " " + request.url.path)
        if request.method == "POST" and request.url.path == "/api/v1/scans":
            return httpx.Response(201, json={"id": "S2"})
        return httpx.Response(200, json={"status": "CREATED", "started": 0.0})

    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    settings = Settings(
        tools=frozenset({"spiderfoot"}),
        spiderfoot_url="http://sf.test",
        spiderfoot_api_key="k",
        spiderfoot_modules=("sfp_accounts",),
        tool_timeout=600,
        proxy=None,
    )
    with pytest.raises(ActionableError, match="did not start it within 180s"):
        t.run("spiderfoot", "exampleuser", settings)
    assert calls[-1] == "POST /api/v1/scans/S2/stop"
    assert sum(c.startswith("GET") for c in calls) < 10  # not 600s of polling


def test_a_long_tool_scan_does_not_stall_other_jobs(engine, source, monkeypatch):
    """Two worker threads: job B completes while job A's tool run is still in progress."""
    import threading

    from harvest.network import Capture

    release = threading.Event()

    def slow_tool(name, target, settings, cancelled=None, top_sites=None, timeout=None):
        release.wait(20)
        url = f"tool://{name}/{target}"
        return Capture(url, url, 200, {"content-type": "application/json"}, b'{"results": []}', 0)

    monkeypatch.setattr("harvest.engine.run_tool", slow_tool)
    engine.settings.tools = frozenset({"maigret"})
    slow = engine.submit(
        JobSpec.model_validate(
            {"objective": "slow scan", "tools": [{"name": "maigret", "target": "exampleuser"}]}
        )
    )
    fast = engine.submit(
        JobSpec.model_validate(
            {"objective": "fast fetch", "seeds": [source["base"] + "/page"], "limits": {"depth": 0}}
        )
    )
    stop = threading.Event()
    threads = [threading.Thread(target=engine.worker, args=(stop,)) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        for _ in range(200):
            if engine.store.job(fast)["status"] == "completed":
                break
            stop.wait(0.1)
        assert engine.store.job(fast)["status"] == "completed"
        assert engine.store.job(slow)["status"] == "running"
    finally:
        release.set()
        stop.set()
        for thread in threads:
            thread.join(10)


def test_orphaned_spiderfoot_scans_are_stopped_only_when_no_task_owns_one(tmp_path, monkeypatch):
    """A worker that dies mid-scan leaves the SpiderFoot scan running; the sweep stops it,
    but never while a live Harvest task is running a scan."""
    calls = []

    def handler(request):
        calls.append(request.method + " " + request.url.path)
        items = [
            {"scan_id": "A1", "name": "harvest-exampleuser", "status": "RUNNING"},
            {"scan_id": "A2", "name": "harvest-example.org", "status": "FINISHED"},
            {"scan_id": "A3", "name": "manual-scan", "status": "RUNNING"},
        ]
        return httpx.Response(200, json={"items": items})

    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    engine, job_id = _tool_engine(tmp_path, seconds=900)
    engine.settings.spiderfoot_url = "http://sf.test"
    engine.settings.spiderfoot_api_key = "k"
    task = engine.store.claim(job_id)  # a live lease: a worker is running this scan
    assert engine.recover_external_scans() == 0 and calls == []
    with engine.store.connection() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    assert engine.recover_external_scans() == 1
    assert calls == ["GET /api/v1/scans", "POST /api/v1/scans/A1/stop"]


def test_a_partial_tool_run_makes_the_job_partial(tmp_path, monkeypatch):
    from harvest.network import Capture

    engine, job_id = _tool_engine(tmp_path, seconds=900)

    def partial_run(name, target, settings, cancelled=None, top_sites=None, timeout=None):
        body = json.dumps({"tool": name, "results": [], "partial": "scan stopped after 30s"})
        url = f"tool://{name}/{target}"
        return Capture(url, url, 200, {"content-type": "application/json"}, body.encode(), 0)

    monkeypatch.setattr("harvest.engine.run_tool", partial_run)
    job = engine.run(job_id)
    assert job["status"] == "partial"
    assert "time allowance" in job["reason"] and "limits.seconds" in job["stop_advice"]


def test_a_tool_run_lost_to_a_worker_restart_says_so(tmp_path):
    engine, job_id = _tool_engine(tmp_path, seconds=900)
    task = engine.store.claim(job_id, lease_seconds=0)  # the worker died holding the lease
    assert task["kind"] == "tool"
    engine.store.claim(job_id)  # the next claim reclaims it: failed, never relaunched
    engine.store.settle(job_id)
    job = engine.store.job(job_id)
    assert job["status"] == "failed"
    assert "worker stopped mid-task" in job["reason"]
    assert "rerun the job" in job["stop_advice"]
