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


def test_retry_after_transient_pass_two_failure_repeats_same_selection(engine, source):
    job, text = six_field_job(engine, source)
    # The 2nd POST is pass 2's first attempt; drop that connection to force an engine retry.
    source["fail_model_attempts"] = {2}
    result = engine.run(job)
    assert result["status"] == "completed" and result["missing_fields"] == []
    assert result["model_calls"] == 3  # Conservative reservation counts the failed call.
    assert source["model_calls"] == 2 and source["counts"]["/report"] == 1
    audits = reserved(engine, job)
    assert [a["reading_pass"] for a in audits] == [1, 2, 2]
    first_pass, failed_attempt, retried_attempt = audits
    # Same durable task, retried once after the dropped connection.
    assert failed_attempt["task"] == retried_attempt["task"]
    assert failed_attempt["attempt"] != retried_attempt["attempt"]
    # The retry must reproduce the exact prompt/passage selection of pass 2's first attempt.
    assert failed_attempt["prompt_sha256"] == retried_attempt["prompt_sha256"]
    assert failed_attempt["selection"]["spans"] == retried_attempt["selection"]["spans"]
    # Pass 1's already-shown spans remain excluded from pass 2.
    shown_first = {(s["start"], s["end"]) for s in first_pass["selection"]["spans"]}
    shown_retry = {(s["start"], s["end"]) for s in retried_attempt["selection"]["spans"]}
    assert not shown_first & shown_retry
    assert any(e["type"] == "deferred" for e in engine.store.events(job))
    for field, fact in zip(FIELDS, FACTS, strict=True):
        observation = next(o for o in engine.store.observations(job) if o["field"] == field)
        assert observation["locator"] == f"text:{text.index(fact)}"


def test_restart_between_passes_resumes_without_repeating_work(engine, source):
    job, _ = six_field_job(engine, source)
    for _ in range(3):  # acquire, extract, reason pass 1
        assert engine.step(job)
    assert source["model_calls"] == 1
    restarted = Engine(engine.settings)
    result = restarted.run(job)
    assert result["status"] == "completed" and result["missing_fields"] == []
    assert source["model_calls"] == 2 and source["counts"]["/report"] == 1


def test_stale_repeated_claims_within_same_job_still_stop_reread(engine, source):
    """The novelty gate must still stop a job that keeps repeating itself. reading_passes is
    set well above what pass one leaves unresolved, so the stop can only come from the
    now-job-scoped novelty check (job_novel==0), not from hitting the configured ceiling.

    Pass two's already-excluded windows mean Reasoner.decide rejects pass one's stale quotes
    as unsupported (they are not in what pass two was actually shown), so this also doubles
    as a same-job "no cherry-picking stale claims" check.
    """
    text = document(list(zip(range(17000, 89000, 12000), FACTS, strict=True)))
    job, _ = six_field_job(engine, source, reading_passes=5, text=text)
    first_claims = {}

    def stubborn_reader(request):
        prompt = json.loads(request["messages"][1]["content"])
        if not first_claims:
            first_claims["claims"] = [
                {"field": field, "value": i + 42, "quote": fact, "confidence": 0.9}
                for i, (field, fact) in enumerate(zip(FIELDS, FACTS, strict=True))
                if fact in prompt["SOURCE_TEXT"]
            ]
            return {"claims": first_claims["claims"], "rationale": "First pass, literal evidence."}
        # Later passes stubbornly repeat pass one's exact claims regardless of what is
        # actually shown now; Reasoner.decide must reject these as unsupported quotes.
        return {"claims": first_claims["claims"], "rationale": "Repeats pass one's claims."}

    source["model_reader"] = stubborn_reader
    result = engine.run(job)
    assert result["status"] == "completed"
    assert len(result["missing_fields"]) == 3  # stuck exactly where pass one left off
    assert source["model_calls"] == 2
    assert [a["reading_pass"] for a in reserved(engine, job)] == [1, 2]


def test_new_job_reread_is_not_blocked_by_an_earlier_jobs_identical_observations(engine, source):
    """Regression for the cross-job novelty bug: `Store.finish` used to gate a job's own
    reread decision on whether an observation was new to the *global* observations table
    (INSERT OR IGNORE dedup is shared across all jobs), so a second, independent job that
    happened to rediscover the same evidence as an earlier job saw zero "novel" observations
    and silently stopped rereading -- even with fields of its own still unresolved. A fresh
    job must judge its own progress by what is new to *its own* extraction, never by whether
    some other job saw the same evidence first.
    """
    text = document(list(zip(range(17000, 89000, 12000), FACTS, strict=True)))

    job1, _ = six_field_job(engine, source, reading_passes=1, text=text)
    result1 = engine.run(job1)
    assert result1["status"] == "completed" and source["model_calls"] == 1
    assert len(result1["missing_fields"]) == 3  # single-pass ceiling: 3 of 6, same as pass=1 alone

    get_count_before = source["counts"]["/report"]
    model_calls_before = source["model_calls"]

    job2, _ = six_field_job(engine, source, reading_passes=3, text=text)
    result2 = engine.run(job2)

    assert result2["status"] == "completed"
    assert result2["missing_fields"] == []  # job2 alone recovers all six
    assert source["model_calls"] - model_calls_before == 2  # exactly two calls for job2
    assert source["counts"]["/report"] - get_count_before == 1  # exactly one GET for job2
    assert [a["reading_pass"] for a in reserved(engine, job2)] == [1, 2]

    # Earlier evidence and cross-job dedup are preserved: job1's exact observation rows are
    # reused (not duplicated) inside job2's own result set.
    job1_ids = {o["id"] for o in engine.store.observations(job1)}
    job2_ids = {o["id"] for o in engine.store.observations(job2)}
    assert job1_ids <= job2_ids
    for field, fact in zip(FIELDS, FACTS, strict=True):
        observation = next(o for o in engine.store.observations(job2) if o["field"] == field)
        assert observation["locator"] == f"text:{text.index(fact)}"


def test_two_sequential_fully_resolved_jobs_each_complete_independently(engine, source):
    """Same bug class, sequential multi-pass jobs: a first job that already fully resolves
    every field (via its own two passes) must not suppress a second, independent job's
    reread either -- the second job's pass=1 rediscovers the same first three facts (already
    globally known from job1) and must still schedule its own pass=2 to reach all six.
    """
    text = document(list(zip(range(17000, 89000, 12000), FACTS, strict=True)))

    job1, _ = six_field_job(engine, source, reading_passes=3, text=text)
    result1 = engine.run(job1)
    assert result1["status"] == "completed" and result1["missing_fields"] == []
    assert source["model_calls"] == 2 and source["counts"]["/report"] == 1

    job2, _ = six_field_job(engine, source, reading_passes=3, text=text)
    result2 = engine.run(job2)
    assert result2["status"] == "completed" and result2["missing_fields"] == []
    assert source["model_calls"] == 4  # two more calls, entirely attributable to job2
    assert source["counts"]["/report"] == 2  # job2's own single GET


def test_second_source_later_pass_reveals_contrary_fact_despite_first_source_satisfying_field(
    engine, source
):
    """Reread scheduling is job-wide (missing fields resolved anywhere end rereading for every
    capture), but that must never suppress or discard a genuinely later pass's disagreeing
    claim: a second, independent source that only reaches a field in its own later pass must
    still have that contrary value preserved as a conflicting candidate, not dropped because
    an earlier source already "satisfied" the field job-wide.
    """
    order = ["revenue", "accreditation", "employees", "founded", "jurisdiction", "ownership"]
    offsets = list(range(17000, 89000, 12000))

    facts_alpha = [
        f"Northstar {field} has recorded value {i + 42}." for i, field in enumerate(order)
    ]
    facts_beta = list(facts_alpha)
    facts_beta[0] = "Northstar revenue has recorded value 999."  # contrary revenue fact

    # Alpha only ever mentions the first three (by original field order); it never has
    # founded/jurisdiction/ownership evidence at all, so its own second pass finds nothing
    # and stops there, leaving those three unresolved job-wide.
    text_alpha = document(list(zip(offsets[:3], facts_alpha[:3], strict=True)))
    # Beta has founded/jurisdiction (but not ownership) plus the contrary revenue fact, so its
    # own pass one -- now reordered by the job-wide missing set -- fills its three ranked
    # slots with founded/jurisdiction/ownership queries, not revenue, and only two of those
    # three are actually findable; "ownership" stays unresolved, motivating beta's own pass 2.
    text_beta = document(
        list(zip(offsets[:2], facts_beta[3:5], strict=True)) + [(offsets[3], facts_beta[0])]
    )

    source["routes"] = {
        "/alpha": (text_alpha, "text/plain"),
        "/beta": (text_beta, "text/plain"),
    }

    def reader(request):
        prompt = json.loads(request["messages"][1]["content"])
        url = prompt["source_url"]
        facts = facts_beta if url.endswith("/beta") else facts_alpha
        claims = [
            {
                "field": field,
                "value": 999 if (field == "revenue" and url.endswith("/beta")) else i + 42,
                "quote": fact,
                "confidence": 0.9,
            }
            for i, (field, fact) in enumerate(zip(order, facts, strict=True))
            if fact in prompt["SOURCE_TEXT"]
        ]
        return {"claims": claims, "rationale": "Literal evidence only."}

    source["model_reader"] = reader
    engine.settings.model_url = source["base"] + "/v1"
    engine.settings.model_name = "fixture-model"
    engine.settings.model_usd_per_million = 1
    spec = JobSpec(
        objective="Investigate Northstar across independent sources",
        seeds=[source["base"] + "/alpha", source["base"] + "/beta"],
        fields=order,
        allowed_domains=["127.0.0.1"],
        use_model=True,
        limits=Limits(domain_delay=0.1, depth=0, reading_passes=2),
    )
    job = engine.submit(spec)
    result = engine.run(job)
    print("STATUS", result["status"], "MISSING", result["missing_fields"])
    for e in reserved(engine, job):
        print("PASS", e.get("reading_pass"), e.get("unresolved_fields"))
    for o in engine.store.observations(job, limit=1000):
        print("OBS", o["field"], o["value"], o["source_url"])
    assert result["status"] == "completed"
    revenue_values = {
        o["value"] for o in engine.store.observations(job, limit=1000) if o["field"] == "revenue"
    }
    # Alpha extracts revenue at index 0 (revenue first in order) → value 42;
    # beta special-cases it as 999. Both values should be preserved.
    assert revenue_values == {42, 999}
