"""Job/result semantics, budget reporting, SpiderFoot lineage, and bounded entity pivots."""

import json
import subprocess
from pathlib import Path

from harvest import tools
from harvest.config import Settings
from harvest.engine import PIVOT_TOP_SITES
from harvest.models import JobSpec
from harvest.store import stop_advice


def test_safe_tool_timeout_default_matches_production(monkeypatch):
    monkeypatch.delenv("HARVEST_TOOL_TIMEOUT", raising=False)
    assert Settings().tool_timeout == 600


def test_investigation_seconds_stop_counting_when_it_finishes(engine):
    job = engine.submit(JobSpec(objective="crawl x", seeds=["https://example.org/a"]))
    with engine.store.transaction() as db:
        db.execute(
            "UPDATE jobs SET status='completed',started=1000.0,finished=1100.0 WHERE id=?", (job,)
        )
    # Was now - started: a job finished long ago reported tens of thousands of seconds used.
    assert engine.store.investigation(job)["enforced"]["seconds"]["used"] == 100.0


def test_cancelling_a_root_cancels_live_children_and_names_what_was_dropped(engine):
    spec = JobSpec(objective="crawl x", seeds=["https://example.org/a", "https://example.org/b"])
    root = engine.submit(spec)
    url = "https://example.org/c"
    child = engine.store.create(
        spec, [{"kind": "fetch", "key": url, "payload": {"url": url}}], parent=root, root=root
    )
    engine.store.stop(root)
    r, c = engine.store.job(root), engine.store.job(child)
    assert r["status"] == c["status"] == "cancelled"
    assert r["reason"] == "operator requested cancellation; 2 unfinished task(s) cancelled"
    assert c["reason"] == f"investigation {root} cancelled; 1 unfinished task(s) cancelled"
    # The appended count must not change which limit the advice names ("task" vs "model").
    assert "model" in stop_advice(
        "budget_exhausted",
        "investigation model_calls limit reached; 3 unfinished task(s) cancelled",
    )


def _search(engine, source, payload):
    source["routes"] = {"/search": (json.dumps(payload), "application/json")}
    engine.settings.search_url = source["base"]


def test_a_search_whose_engines_were_down_fails_instead_of_finding_nothing(engine, source):
    down = [["bing", "HTTP connection error"], ["brave", "unexpected crash"]]
    _search(engine, source, {"results": [], "unresponsive_engines": down})
    spec = JobSpec(objective="find jane", discovery_queries=['"jane"'])
    job = engine.run(engine.submit(spec))
    assert job["status"] == "failed"
    assert "engines were unresponsive (bing, brave)" in job["reason"]
    assert "not evidence of absence" in job["reason"]


def _maigret_report(monkeypatch, report, argvs=None):
    def fake_exec(argv, timeout, cwd, cancelled=None, env=None):
        if argvs is not None:
            argvs.append(argv)
        workdir = argv[argv.index("--folderoutput") + 1]
        Path(f"{workdir}/report_{argv[1]}_simple.json").write_text(json.dumps(report))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(tools, "_exec", fake_exec)


def test_identifier_searches_drop_results_that_never_mention_the_identifier(
    engine, source, monkeypatch
):
    base = source["base"]
    _search(
        engine,
        source,
        {
            "results": [
                {"url": f"{base}/hit", "title": "exampleuser on a forum"},
                {"url": f"{base}/noise", "title": "Real big cruise deals", "content": "unrelated"},
            ],
            "unresponsive_engines": [["wikidata", "parsing error"]],
        },
    )
    source["routes"].update(
        {
            "/hit": ("<html><title>exampleuser</title></html>", "text/html"),
            "/noise": ("<html><title>cruise</title></html>", "text/html"),
        }
    )
    source["robots"] = "User-agent: *\nAllow: /\n"
    engine.settings.tools = frozenset({"maigret"})
    _maigret_report(monkeypatch, {})
    spec = JobSpec.model_validate(
        {
            "objective": "Investigate exampleuser",
            "allowed_domains": ["127.0.0.1"],
            "discovery_queries": ['"exampleuser"'],
            "tools": [{"name": "maigret", "target": "exampleuser", "crawl": False}],
            "limits": {"domain_delay": 0.1},
        }
    )
    job = engine.run(engine.submit(spec))
    assert job["status"] == "completed" and job["reason"] == "all queued work finished"
    requested = [r.split("?")[0] for r in source["requests"]]
    assert "/hit" in requested and "/noise" not in requested
    done = [e["details"] for e in engine.store.events(job["id"]) if "query" in e["details"]]
    assert done[0]["not_mentioning_identifier"] == 1
    # A partial outage with results is still a finding, with the outage on record.
    assert done[0]["unresponsive_engines"] == ["wikidata"]
    # ...and it reaches the summary instead of staying buried in task events.
    summary = engine.store.job_summary(job["id"])
    assert summary["search"] == {
        "runs": 1, "degraded": 1, "unresponsive_engines": ["wikidata"],
        "results": 2, "accepted_leads": 1,
    }
    assert any(u.startswith("discovery search degraded: 1/1") for u in summary["unknowns"])


def _sf_events():
    target = "jane@example.org"
    url = "<SFURL>https://site.example/{}</SFURL>"
    return target, [
        {"hash": "r", "type": "ROOT", "data": target},
        {"hash": "e", "type": "EMAILADDR", "data": target, "source_event_hash": "r"},
        {"hash": "u", "type": "USERNAME", "data": "jane", "module": "sfp_accounts", "source_event_hash": "e"},
        {"hash": "n", "type": "HUMAN_NAME", "data": "Jane Roe", "module": "sfp_tiktok_osint", "source_event_hash": "u"},
        {"hash": "u2", "type": "USERNAME", "data": "janeroe", "module": "sfp_accounts", "source_event_hash": "n"},
        {"hash": "a2", "type": "ACCOUNT_EXTERNAL_OWNED", "data": "Site\n" + url.format("janeroe"), "module": "sfp_accounts", "source_event_hash": "u2"},
        {"hash": "a1", "type": "ACCOUNT_EXTERNAL_OWNED", "data": "Site\n" + url.format("jane"), "module": "sfp_accounts", "source_event_hash": "u"},
    ]  # fmt: skip


def test_spiderfoot_findings_reached_through_a_name_are_labelled_not_attributed():
    target, events = _sf_events()
    by_data = {
        r["data"] if "url" not in r else r["url"]: r
        for r in tools._spiderfoot_records(events, target)
    }
    # Off the target's own identifiers: sfp_accounts swept handles built from a display name.
    for key in ("janeroe", "https://site.example/janeroe"):
        assert by_data[key]["pivoted_via"] == "HUMAN_NAME", key
        assert by_data[key]["_confidence"] <= 0.5 and by_data[key]["ownership"] == "candidate"
    # The handle derived from the address, its account, and the name itself are on-target.
    for key in ("jane", "https://site.example/jane", "Jane Roe"):
        assert "pivoted_via" not in by_data[key], key
    # Only the one-hop handle is worth a pivot; never the name-derived one.
    found = tools.pivot_candidates("spiderfoot", list(by_data.values()), target)
    assert [(k, v) for k, v, _ in found] == [("username", "jane")]
    assert found[0][2] == {"tool": "spiderfoot", "module": "sfp_accounts"}


def test_maigret_pivot_candidates_are_observed_one_hop_and_never_the_input():
    records = [
        {
            "sitename": "Threads",
            "url": "https://threads.example/@exampleuser",
            "existence": "observed",
            "fullname": "Example Name",
            "contact": "ex@example.org",
            "ids_usernames": {"exalt": "username", "ExampleUser": "username"},
        },
        {"sitename": "Weak", "existence": "inferred", "fullname": "Someone Else", "ids_usernames": {"weak1": "x"}},
        {"sitename": "Dup", "existence": "observed", "fullname": "example name"},
    ]  # fmt: skip
    found = tools.pivot_candidates("maigret", records, "exampleuser")
    assert [(k, v) for k, v, _ in found] == [
        ("email", "ex@example.org"),
        ("username", "exalt"),
        ("person", "Example Name"),
    ]
    assert found[0][2]["field"] == "contact" and found[2][2]["site"] == "Threads"
    assert tools.pivot_candidates("ghunt", records, "exampleuser") == []


_PROFILE_REPORT = {
    "Threads": {
        "url_user": "https://threads.example/@exampleuser",
        "http_status": 200,
        "status": {"status": "Claimed", "ids": {"fullname": "Example Name"}},
        "ids_usernames": {"exalt": "username"},
        "site": {"name": "Threads", "checkType": "message"},
    }
}


def _pivot_engine(engine, source, monkeypatch, argvs):
    engine.settings.tools = frozenset({"maigret"})
    _search(engine, source, {"results": []})
    _maigret_report(monkeypatch, _PROFILE_REPORT, argvs)
    return engine


def _spec(**limits):
    return JobSpec.model_validate(
        {
            "objective": "Investigate exampleuser",
            "tools": [{"name": "maigret", "target": "exampleuser", "crawl": False}],
            "limits": {"domain_delay": 0.1, **limits},
        }
    )


def test_pivots_enrich_discovered_identifiers_in_their_own_jobs(engine, source, monkeypatch):
    argvs = []
    _pivot_engine(engine, source, monkeypatch, argvs)
    root = engine.run(engine.submit(_spec(pivots=3, tool_runs=3)))
    events = engine.store.events(root["id"])
    pivots = [e["details"] for e in events if e["type"] == "pivot"]
    assert [(p["kind"], p["value"]) for p in pivots] == [
        ("username", "exalt"),
        ("person", "Example Name"),
    ]
    assert pivots[0]["source"] == {
        "tool": "maigret",
        "site": "Threads",
        "url": "https://threads.example/@exampleuser",
        "field": "ids_usernames",
    }
    assert pivots[1]["queries"] == ['"Example Name" "exampleuser"'] and pivots[1]["tools"] == []
    for p in pivots:
        child = engine.run(p["child"])
        assert child["root_id"] == child["parent_id"] == root["id"]
    username = engine.store.job(pivots[0]["child"])["spec"]["tools"]
    assert username == [
        {"name": "maigret", "target": "exalt", "crawl": False, "top_sites": PIVOT_TOP_SITES}
    ]
    assert argvs[1][argvs[1].index("--top-sites") + 1] == str(PIVOT_TOP_SITES)
    # One hop: a pivot's own discoveries (the same report) never pivot again.
    for p in pivots:
        assert not [e for e in engine.store.events(p["child"]) if e["type"] == "pivot"]
    # The root's accounts are the root's; the pivot's live under the pivot.
    summary = engine.store.job_summary(root["id"])
    assert [a["url"] for a in summary["accounts"]] == ["https://threads.example/@exampleuser"]
    rows = {p["value"]: p for p in summary["pivots"]}
    assert rows["exalt"]["status"] == "completed" and len(rows["exalt"]["accounts"]) == 1
    assert summary["tool_runs"] == "2/3" and "not metered" in summary["tool_requests"]
    assert [p["value"] for p in engine.store.investigation(root["id"])["pivots"]] == [
        "exalt",
        "Example Name",
    ]


def test_pivots_are_off_by_default_bounded_and_refused_past_the_budget(engine, source, monkeypatch):
    _pivot_engine(engine, source, monkeypatch, [])
    off = engine.run(engine.submit(_spec()))
    assert not [e for e in engine.store.events(off["id"]) if e["type"].startswith("pivot")]

    one = engine.run(engine.submit(_spec(pivots=1, tool_runs=3)))
    assert len([e for e in engine.store.events(one["id"]) if e["type"] == "pivot"]) == 1

    # tool_runs=1 is spent by the root's own run: the handle pivot is refused, with the
    # reason on record, and the job is not stopped by it; the search-only name pivot runs.
    tight = engine.run(engine.submit(_spec(pivots=3, tool_runs=1)))
    events = engine.store.events(tight["id"])
    skipped = [e["details"] for e in events if e["type"] == "pivot_skipped"]
    assert skipped[0]["value"] == "exalt" and "tool_runs" in skipped[0]["reason"]
    assert [e["details"]["kind"] for e in events if e["type"] == "pivot"] == ["person"]
    assert tight["status"] == "completed"


def test_json_api_captures_only_expand_through_links_carrying_the_identifier(
    engine, source, monkeypatch
):
    # GitHub's API is extracted in batches, so it never gets a page check; its links used to
    # be followed wholesale, into received_events and other users' repositories.
    base = source["base"]
    api = {
        "login": "exampleuser",
        "repos_url": f"{base}/api/users/exampleuser/repos",
        "received_events_url": f"{base}/api/user/123/received_events",
        "starred": f"{base}/api/repos/someoneelse/tool",
    }
    source["routes"] = {
        "/api/users/exampleuser": (json.dumps(api), "application/json"),
        "/api/users/exampleuser/repos": ("[]", "application/json"),
        "/api/user/123/received_events": ("[]", "application/json"),
        "/api/repos/someoneelse/tool": ("{}", "application/json"),
    }
    source["robots"] = "User-agent: *\nAllow: /\n"
    engine.settings.tools = frozenset({"maigret"})
    report = {
        "Code": {
            "url_user": f"{base}/api/users/exampleuser",
            "http_status": 200,
            "status": {"status": "Claimed"},
            "site": {"name": "Code", "checkType": "message"},
        }
    }
    _maigret_report(monkeypatch, report)
    spec = JobSpec.model_validate(
        {
            "objective": "Investigate exampleuser",
            "allowed_domains": ["127.0.0.1"],
            "tools": [{"name": "maigret", "target": "exampleuser"}],
            "limits": {"domain_delay": 0.1, "depth": 3},
        }
    )
    engine.run(engine.submit(spec))
    paths = [r.split("?")[0] for r in source["requests"] if not r.startswith("/robots.txt")]
    assert "/api/users/exampleuser" in paths and "/api/users/exampleuser/repos" in paths
    assert "/api/user/123/received_events" not in paths
    assert "/api/repos/someoneelse/tool" not in paths
