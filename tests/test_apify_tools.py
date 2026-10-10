import json
import threading

import httpx
import pytest
from pydantic import ValidationError

from harvest import tools
from harvest.config import Settings
from harvest.models import ActionableError, LostLease, PolicyDenied, ToolRun

DORK = 'site:linkedin.com/in "Jane Doe"'


def _settings(**kw):
    return Settings(tools=frozenset(tools._APIFY), apify_token="tok-secret", **kw)


@pytest.fixture
def api(monkeypatch):
    """Route tools.py's httpx.Client through a mock transport and record the requests."""
    seen, reply = [], {"status": 200, "json": []}
    real = httpx.Client

    def handler(request):
        seen.append(request)
        return httpx.Response(reply["status"], json=reply["json"])

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **{**kw, "proxy": None}),
    )
    return seen, reply


def test_free_text_target_only_for_apify_tools():
    ToolRun(name="apify_google", target=DORK)
    ToolRun(name="apify_google", target="Jane Doe")
    for bad in (" Jane", "Jane ", "Jane\nDoe", "x" * 255):
        with pytest.raises(ValidationError):
            ToolRun(name="apify_google", target=bad)
    for name in ("maigret", "ghunt", "spiderfoot"):
        with pytest.raises(ValidationError):
            ToolRun(name=name, target="Jane Doe")
    ToolRun(name="maigret", target="janedoe")


def test_google_dork_is_one_capped_authenticated_call(api):
    seen, reply = api
    reply["json"] = [
        {
            "organicResults": [
                {"title": "Jane Doe", "url": "https://linkedin.com/in/jane", "position": 1},
                {"title": "no url"},
            ],
            "relatedQueries": [{"title": "dropped"}],
        }
    ]
    capture = tools.run("apify_google", DORK, _settings())
    body = json.loads(capture.body)
    assert capture.url == f"tool://apify_google/{DORK}"
    assert [r["url"] for r in body["results"]] == ["https://linkedin.com/in/jane"]
    assert body["results"][0]["query"] == DORK
    (request,) = seen
    assert request.headers["authorization"] == "Bearer tok-secret"
    assert request.url.params["maxTotalChargeUsd"] == "0.5"
    assert json.loads(request.content) == {"queries": DORK, "maxPagesPerQuery": 1}


def test_tiktok_profile_is_deduplicated_across_video_rows(api):
    _, reply = api
    meta = {"id": "u1", "name": "nasa", "fans": 5}
    reply["json"] = [{"id": "v1", "authorMeta": meta}, {"id": "v2", "authorMeta": meta}]
    records = json.loads(tools.run("apify_tiktok", "nasa", _settings()).body)["results"]
    assert [(r["dataType"], r["id"]) for r in records] == [
        ("user_profile", "u1"),
        ("video", "v1"),
        ("video", "v2"),
    ]


def test_error_rows_are_dropped_and_attribution_is_candidate(api):
    _, reply = api
    reply["json"] = [{"id": "1", "username": "a"}, {"error": "not_found"}]
    records = json.loads(tools.run("apify_instagram", "a", _settings()).body)["results"]
    assert len(records) == 1
    out = tools.attribute_record("apify_instagram", records[0], "a")
    assert (out["existence"], out["ownership"]) == ("observed", "candidate")


@pytest.mark.parametrize(
    "status,error", [(401, PolicyDenied), (402, ActionableError), (500, ActionableError)]
)
def test_failures_are_actionable_and_never_echo_the_token(api, status, error):
    _, reply = api
    reply["status"] = status
    with pytest.raises(error) as caught:
        tools.run("apify_reddit", "spez", _settings())
    assert "tok-secret" not in str(caught.value)


def test_requires_token_and_honours_cancel_before_calling(api):
    seen, _ = api
    with pytest.raises(PolicyDenied):
        tools.run("apify_reddit", "spez", Settings(tools=frozenset(tools._APIFY)))
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(LostLease):
        tools.run("apify_reddit", "spez", _settings(), cancelled=cancelled)
    assert not seen


def test_plans_offer_apify_tools_only_to_a_ready_thorough_run():
    from harvest.planning import effective_plan, plan_investigation

    person = plan_investigation("Jane Doe", "person")
    assert [t["target"] for t in person.tools] == [
        '"Jane Doe"',
        'site:linkedin.com/in "Jane Doe"',
        '"Jane Doe" filetype:pdf OR filetype:docx',
    ]
    assert {t["name"] for t in person.tools} == {"apify_google"}
    username = plan_investigation("janedoe", "username").tools
    names = {t["name"] for t in username}
    assert {"apify_instagram", "apify_tiktok", "apify_reddit", "apify_google"} <= names
    assert "apify_linkedin" not in names  # explicit only
    email = plan_investigation("jane@x.org", "email").tools
    assert '"jane@x.org" -site:x.org' in {t["target"] for t in email}
    assert plan_investigation("x" * 260, "person").tools == []
    # A quote in a name cannot break out of the phrase.
    assert plan_investigation('Jane "Q" Doe', "person").tools[0]["target"] == '"Jane Q Doe"'

    ready = _settings()
    deep = effective_plan("Jane Doe", "person", "deep", ready)
    assert {t["name"] for t in deep["tools"]} == {"apify_google"}
    assert effective_plan("Jane Doe", "person", "standard", ready)["tools"] == []  # paid: deep only
    unready = Settings(tools=frozenset(tools._APIFY))  # no token
    blocked = effective_plan("Jane Doe", "person", "deep", unready)
    assert blocked["tools"] == []
    assert blocked["unavailable_tools"][0]["detail"] == "HARVEST_APIFY_TOKEN is not set"


def test_every_thorough_plan_fits_its_tool_run_budget():
    from harvest.planning import effective_plan, presets

    deep = presets()["deep"]
    every_tool = Settings(
        tools=frozenset(tools.TOOLS),
        apify_token="t",
        spiderfoot_url="http://sf",
        spiderfoot_api_key="k",
        spiderfoot_modules=("sfp_x",),
    )
    for value, kind in (("janedoe", "username"), ("jane@x.org", "email"), ("Jane Doe", "person")):
        plan = effective_plan(value, kind, "deep", every_tool)
        assert len(plan["tools"]) <= deep["tool_runs"], (kind, plan["tools"])


def test_username_pivots_include_the_apify_platforms_only_with_a_token():
    from harvest.engine import PIVOT_TOOLS

    assert {"apify_instagram", "apify_tiktok", "apify_reddit"} <= set(PIVOT_TOOLS["username"])
    assert "apify_google" not in PIVOT_TOOLS["username"]  # names/dorks stay on SearXNG


class _Recorder:
    def __init__(self):
        self.checks = []

    def check(self, name, outcome, reason=""):
        self.checks.append((name, outcome, reason))


@pytest.mark.parametrize(
    "rows,outcome",
    [
        ([{"error": "does not exist", "errorCode": "NOT_FOUND", "url": "u"}], "absent"),
        ([{"error": "blocked", "errorCode": "RATE_LIMITED"}], "errored"),
        ([], "absent"),
        ([{"id": "v1", "authorMeta": {"id": "u1"}}], "found"),
    ],
)
def test_lookup_outcome_is_reported_and_error_rows_are_never_records(api, rows, outcome):
    _, reply = api
    reply["json"] = rows
    recorder = _Recorder()
    marker = tools._RECORDER.set(recorder)
    try:
        records = json.loads(tools.run("apify_tiktok", "nasa", _settings()).body)["results"]
    finally:
        tools._RECORDER.reset(marker)
    assert [c[:2] for c in recorder.checks] == [("lookup", outcome)]
    assert all("error" not in r for r in records)
