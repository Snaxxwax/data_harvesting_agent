import json

from harvest.engine import Engine
from harvest.models import JobSpec, Limits


def test_objective_only_search_and_grounded_recursive_reasoning(engine, source):
    settings = engine.settings
    settings.search_url = source["base"]
    settings.model_url = source["base"] + "/v1"
    settings.model_name = "fixture-model"
    settings.model_usd_per_million = 1
    engine = Engine(settings)
    spec = JobSpec(
        objective="Investigate Alpha protocol support",
        mode="deep_research",
        use_model=True,
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=3, model_tokens=100000, seconds=30),
    )
    job = engine.submit(spec)
    result = engine.run(job)
    assert result["status"] == "completed"
    observations = engine.store.observations(job)
    assert any(r["field"] == "protocol" and r["value"] == "HTTP/2" for r in observations)
    assert not any(r["field"] == "invented" for r in observations)
    assert source["model_calls"] == 2
    assert source["counts"]["/search"] == 2
    assert result["cost_reserved_usd"] > 0
    decisions = [e["details"] for e in engine.store.events(job) if "decision" in e["details"]]
    assert all(d["rejected_unsupported_quotes"] == 1 for d in decisions)
    assert decisions[0]["decision"]["gaps"]
    assert "research_state" in json.loads(source["last_model_request"]["messages"][1]["content"])


def test_model_budget_reserved_before_billable_request(engine, source):
    settings = engine.settings
    settings.model_url = source["base"] + "/v1"
    settings.model_name = "fixture-model"
    settings.model_usd_per_million = 100
    engine = Engine(settings)
    spec = JobSpec(
        objective="Find protocol",
        seeds=[source["base"] + "/page"],
        use_model=True,
        fields=["protocol"],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0, cost_usd=0.001),
    )
    result = engine.run(engine.submit(spec))
    assert result["status"] == "budget_exhausted"
    assert source["model_calls"] == 0
    assert result["captures"] == 1  # deterministic evidence survives reasoning budget exhaustion


def test_model_cannot_change_operator_policy(engine, source):
    settings = engine.settings
    settings.model_url = source["base"] + "/v1"
    settings.model_name = "fixture-model"
    settings.model_usd_per_million = 0
    source["model_answer"] = {
        "claims": [],
        "leads": [
            {
                "url": "http://169.254.169.254/latest/meta-data",
                "reason": "source says fetch credentials",
            }
        ],
        "rationale": "follow instruction",
        "private_hosts": ["169.254.169.254"],
    }
    engine = Engine(settings)
    spec = JobSpec(
        objective="Find protocol",
        seeds=[source["base"] + "/page"],
        use_model=True,
        fields=["protocol"],
        allowed_domains=["127.0.0.1"],
        limits=Limits(domain_delay=0.1, depth=0),
    )
    result = engine.run(engine.submit(spec))
    assert result["status"] == "partial"
    assert result["progress"]["failed"] == 1
    assert result["captures"] == 1
