"""Durable multi-pass reading: unresolved requested fields trigger a bounded reread."""

import json

from harvest.engine import Engine
from harvest.models import JobSpec, Limits

FIELDS = ["accreditation", "employees", "jurisdiction", "revenue", "founded", "ownership"]
FACTS = [f"Northstar {field} has recorded value {i + 42}." for i, field in enumerate(FIELDS)]


def document(insertions, length=95000):
    text = ("Routine audit methodology and appendix. " * 3000)[:length]
    for offset, quote in sorted(insertions, reverse=True):
        text = text[:offset] + quote + text[offset + len(quote) :]
    return text


def scripted_reader(request):
    """Emit a claim for every labeled fact whose literal quote reached SOURCE_TEXT."""
    prompt = json.loads(request["messages"][1]["content"])
    claims = [
        {"field": field, "value": i + 42, "quote": fact, "confidence": 0.9}
        for i, (field, fact) in enumerate(zip(FIELDS, FACTS, strict=True))
        if fact in prompt["SOURCE_TEXT"]
    ]
    return {"claims": claims, "rationale": "Literal evidence only."}


def six_field_job(engine, source, reading_passes=3, text=None):
    text = text or document(list(zip(range(17000, 89000, 12000), FACTS, strict=True)))
    source["routes"] = {"/report": (text, "text/plain")}
    source["model_reader"] = scripted_reader
    engine.settings.model_url = source["base"] + "/v1"
    engine.settings.model_name = "fixture-model"
    engine.settings.model_usd_per_million = 1
    spec = JobSpec(
        objective="Investigate Northstar",
        seeds=[source["base"] + "/report"],
        fields=FIELDS,
        allowed_domains=["127.0.0.1"],
        use_model=True,
        limits=Limits(domain_delay=0.1, depth=0, reading_passes=reading_passes),
    )
    return engine.submit(spec), text


def reserved(engine, job):
    return [e["details"] for e in engine.store.events(job) if e["type"] == "model_reserved"]


def test_single_pass_ceiling_is_three_of_six(engine, source):
    job, _ = six_field_job(engine, source, reading_passes=1)
    result = engine.run(job)
    assert result["status"] == "completed" and source["model_calls"] == 1
    assert len(result["missing_fields"]) == 3
    assert [e["reading_pass"] for e in reserved(engine, job)] == [1]


def test_second_pass_resolves_remaining_fields_without_new_get(engine, source):
    job, text = six_field_job(engine, source)
    result = engine.run(job)
    assert result["status"] == "completed" and result["missing_fields"] == []
    assert source["model_calls"] == 2 and source["counts"]["/report"] == 1
    audits = reserved(engine, job)
    assert [a["reading_pass"] for a in audits] == [1, 2]
    assert len(audits[1]["unresolved_fields"]) == 3 and audits[1]["excluded_span_count"] > 0
    shown_first = {(s["start"], s["end"]) for s in audits[0]["selection"]["spans"]}
    shown_second = {(s["start"], s["end"]) for s in audits[1]["selection"]["spans"]}
    assert not shown_first & shown_second
    prompt = json.loads(audits[1]["user_prompt"])
    assert prompt["requested_fields"] == audits[1]["unresolved_fields"]
    for field, fact in zip(FIELDS, FACTS, strict=True):
        observation = next(o for o in engine.store.observations(job) if o["field"] == field)
        assert observation["locator"] == f"text:{text.index(fact)}"
    # Idempotent rerun: no new GET, no new model call.
    assert engine.run(job)["status"] == "completed" and source["model_calls"] == 2


def test_zero_novel_pass_stops_rereading(engine, source):
    job, _ = six_field_job(engine, source)
    source["model_reader"] = lambda request: {"claims": [], "rationale": "Nothing supported."}
    result = engine.run(job)
    assert result["status"] == "completed" and source["model_calls"] == 1
    assert len(result["missing_fields"]) == 6


def test_fully_shown_document_is_not_reread(engine, source):
    short = document([(200, FACTS[0]), (900, FACTS[1])], length=6000)
    job, _ = six_field_job(engine, source, text=short)
    result = engine.run(job)
    assert result["status"] == "completed" and source["model_calls"] == 1
    assert len(result["missing_fields"]) == 4
    done = [e["details"] for e in engine.store.events(job) if e["type"] == "task_done"]
    assert any(d.get("coverage_exhausted") for d in done)


def test_pass_cap_bounds_model_calls(engine, source):
    job, _ = six_field_job(engine, source, reading_passes=2)
    source["model_reader"] = lambda request: {
        "claims": [
            {
                "field": "accreditation",
                "value": "x",
                "quote": "Routine audit methodology and appendix.",
                "confidence": 0.5,
            }
        ],
        "rationale": "Repeats the same claim every pass.",
    }
    result = engine.run(job)
    assert result["status"] == "completed" and source["model_calls"] == 2


def test_restart_between_passes_resumes_without_repeating_work(engine, source):
    job, _ = six_field_job(engine, source)
    for _ in range(3):  # acquire, extract, reason pass 1
        assert engine.step(job)
    assert source["model_calls"] == 1
    restarted = Engine(engine.settings)
    result = restarted.run(job)
    assert result["status"] == "completed" and result["missing_fields"] == []
    assert source["model_calls"] == 2 and source["counts"]["/report"] == 1
