import json
import sqlite3
from contextlib import contextmanager

import pytest

from harvest.engine import Engine
from harvest.extract import HtmlAdapter, TextAdapter
from harvest.models import JobSpec, Limits
from harvest.passages import SEPARATOR, select_passages
from harvest.store import digest


@pytest.mark.parametrize("offset", [0, 2190, 13990, 37300, 89000])
def test_fact_at_varied_offsets_with_original_unicode_coordinates(offset):
    quote = "Étoile accreditation is ISO 27001."
    text = "日 " * (offset // 2) + quote + " ordinary methodology" * 4500
    text = text[:100000]
    selected = select_passages(text, "Investigate Étoile", ["accreditation"], {})
    assert quote in selected.text
    assert selected.locate(quote) == text.index(quote)
    assert len(selected.text) <= 14000
    assert selected.metadata["selected_chars"] + selected.metadata["omitted_chars"] == len(text)
    for span in selected.spans:
        assert (
            selected.text[span["input_start"] : span["input_end"]]
            == text[span["start"] : span["end"]]
        )


def test_distant_contradictions_survive_repetitive_boilerplate():
    first = "Northstar accreditation was granted in 2024."
    second = "Northstar accreditation was revoked in 2026."
    text = (
        "Northstar methodology. " * 1600
        + first
        + " routine audit. " * 1800
        + second
        + " appendix" * 2500
    )
    selected = select_passages(text, "Northstar accreditation", ["accreditation"], {})
    assert first in selected.text and second in selected.text
    assert selected.locate(second) == text.index(second)


def test_multiple_fields_receive_passages():
    facts = ["accreditation is ISO 27001.", "employees total 42.", "jurisdiction is GB."]
    text = "introduction " * 1800
    for fact in facts:
        text += fact + " method detail " * 1200
    selected = select_passages(
        text, "Investigate company", ["accreditation", "employees", "jurisdiction"], {}
    )
    assert all(fact in selected.text for fact in facts)


def test_short_text_unchanged_and_empty_text_supported():
    for text in ("", "Alpha supports HTTP/2.", "x" * 14000):
        selected = select_passages(text, "objective", [], {})
        assert selected.text == text and selected.metadata["omitted_chars"] == 0


def test_quotes_cannot_bridge_omissions_or_use_markers():
    selected = select_passages("ordinary content " * 6000, "nothing", [], {})
    assert len(selected.spans) > 1
    boundary = selected.spans[0]["input_end"]
    invented = selected.text[boundary - 10 : boundary + len(SEPARATOR) + 10]
    assert invented in selected.text
    assert selected.locate(invented) is None
    assert selected.locate(SEPARATOR) is None


def test_fts_syntax_is_literal_and_selection_is_repeatable():
    text = "normal prose " * 3000 + "accreditation: ISO 27001" + " appendix " * 1000
    objective = '" OR NEAR(*) ; DROP TABLE passages; --'
    a = select_passages(text, objective, ["accreditation"], {})
    b = select_passages(text, objective, ["accreditation"], {})
    assert a == b and "accreditation: ISO 27001" in a.text


def test_no_fts5_has_explicit_coverage_fallback(monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr("harvest.passages.rank_windows", unavailable)
    selected = select_passages("prose " * 15000, "objective", [], {})
    assert selected.metadata["backend"] == "coverage-fallback-no-fts5"
    assert len(selected.spans) == 5 and len(selected.text) <= 14000


@pytest.mark.parametrize(
    "adapter,body",
    [(TextAdapter(), b"x" * 100001), (HtmlAdapter(), b"<p>" + b"x" * 100001 + b"</p>")],
)
def test_adapter_text_truncation_is_disclosed(adapter, body):
    result = adapter.extract(body, "https://example.org/report")
    assert len(result.text) == 100000 and result.warnings


def configured_job(engine, source, media="text/plain", mode="targeted"):
    quote = "Étoile accreditation is ISO 27001."
    text = "Audit methodology. " * 1900 + quote + " appendix " * 1500
    body = "<p>" + text + "</p>" if media == "text/html" else text
    source["routes"] = {"/report": (body, media)}
    source["model_answer"] = {
        "claims": [
            {"field": "accreditation", "value": "ISO 27001", "quote": quote, "confidence": 0.9}
        ],
        "rationale": "Literal evidence.",
    }
    engine.settings.model_url = source["base"] + "/v1"
    engine.settings.model_name = "fixture-model"
    engine.settings.model_usd_per_million = 1
    spec = JobSpec(
        objective="Investigate Étoile",
        seeds=[source["base"] + "/report"],
        fields=["accreditation"],
        allowed_domains=["127.0.0.1"],
        use_model=True,
        mode=mode,
        limits=Limits(domain_delay=0.1, depth=0),
    )
    return engine.submit(spec), text, quote, spec


@pytest.mark.parametrize("media", ["text/plain", "text/html"])
def test_real_http_long_document_audit_and_idempotency(engine, source, media):
    job, text, quote, spec = configured_job(engine, source, media)
    result = engine.run(job)
    assert result["status"] == "completed" and source["model_calls"] == 1
    observation = next(o for o in engine.store.observations(job) if o["field"] == "accreditation")
    assert observation["locator"] == f"text:{text.index(quote)}"
    assert observation["capture_ids"] and observation["extraction_ids"]
    audit = next(e["details"] for e in engine.store.events(job) if e["type"] == "model_reserved")
    prompt = json.loads(audit["user_prompt"])
    assert prompt == json.loads(source["last_model_request"]["messages"][1]["content"])
    assert digest(audit["system_prompt"] + audit["user_prompt"]) == audit["prompt_sha256"]
    normalized = text.strip() if media == "text/html" else text
    assert audit["selection"]["normalized_text_sha256"] == digest(normalized)
    assert audit["body_sha256"] == engine.store.captures(job)[0]["body_hash"]
    assert audit["normalizer"] in {"html-jsonld/1", "text/1"}
    assert quote in prompt["SOURCE_TEXT"] and audit["selection"]["omitted_chars"] > 0
    before = dict(source["counts"])
    assert engine.run(job)["status"] == "completed"
    assert dict(source["counts"]) == before and source["model_calls"] == 1


def test_input_audit_survives_crash_before_provider_and_restart(engine, source):
    job, _, _, _ = configured_job(engine, source)
    engine.step(job)  # acquire
    engine.step(job)  # extract
    task = engine.store.claim(job)

    class CrashingClient:
        @contextmanager
        def stream(self, *args, **kwargs):
            audits = [e for e in engine.store.events(job) if e["type"] == "model_reserved"]
            assert len(audits) == 1 and audits[0]["details"]["user_prompt"]
            raise SystemExit("crash before send")
            yield  # pragma: no cover

    engine.reasoner.client = CrashingClient()
    with pytest.raises(SystemExit, match="crash before send"):
        engine.process(task)
    with engine.store.transaction() as db:
        db.execute("UPDATE tasks SET lease_until=0 WHERE id=?", (task["id"],))
    restarted = Engine(engine.settings)
    assert restarted.run(job)["status"] == "completed"
    audits = [e["details"] for e in restarted.store.events(job) if e["type"] == "model_reserved"]
    assert [a["attempt"] for a in audits] == [1, 2]
    assert audits[0]["user_prompt"] == audits[1]["user_prompt"]
    assert restarted.store.job(job)["model_calls"] == 2  # conservative reservations, no refund
    assert source["model_calls"] == 1 and source["counts"]["/report"] == 1


def test_long_document_refresh_preserves_prior_evidence(engine, source):
    job, text, quote, _ = configured_job(engine, source, mode="continuous")
    assert engine.run(job)["status"] == "completed"
    source["routes"]["/report"] = (
        text.replace(quote, "Étoile accreditation was revoked."),
        "text/plain",
    )
    source["model_answer"]["claims"][0].update(
        value="revoked", quote="Étoile accreditation was revoked."
    )
    with engine.store.transaction() as db:
        db.execute("UPDATE schedules SET next_run=0 WHERE id=?", (job,))
    engine.store.schedule_tick()
    child = next(j["id"] for j in engine.store.jobs() if j["id"] != job)
    assert engine.run(child)["status"] == "completed"
    assert engine.store.canonical("default")[0]["fields"]["accreditation"]["value"] == "revoked"
    assert engine.store.observations(job)[0]["value"] == "ISO 27001"
    assert source["model_calls"] == 2


def test_model_quote_crossing_omissions_is_rejected_end_to_end(engine, source):
    job, text, _, spec = configured_job(engine, source)
    selection = select_passages(text, spec.objective, spec.fields, {})
    boundary = selection.spans[0]["input_end"]
    bridge = selection.text[boundary - 5 : boundary + len(SEPARATOR) + 5]
    source["model_answer"]["claims"][0]["quote"] = bridge
    result = engine.run(job)
    assert result["missing_fields"] == ["accreditation"]
    assert engine.store.observations(job) == []
    decision = next(e["details"] for e in engine.store.events(job) if "decision" in e["details"])
    assert decision["rejected_unsupported_quotes"] == 1


def test_corrupt_capture_cannot_be_sent_to_model(engine, source):
    job, _, _, _ = configured_job(engine, source)
    engine.step(job)
    engine.step(job)
    with engine.store.transaction() as db:
        db.execute("UPDATE blobs SET body=?", (b"tampered evidence",))
    result = engine.run(job)
    assert result["status"] == "partial" and source["model_calls"] == 0
    assert not any(e["type"] == "model_reserved" for e in engine.store.events(job))


def test_missing_fields_do_not_depend_on_observation_sample(engine, source, monkeypatch):
    job, _, _, _ = configured_job(engine, source)
    assert engine.run(job)["missing_fields"] == []
    monkeypatch.setattr(engine.store, "observations", lambda *args, **kwargs: [])
    assert engine.context(job)["missing_fields"] == []


def test_known_missing_fields_get_priority_over_already_observed_fields():
    from harvest.passages import queries_for

    queries = queries_for(
        "Investigate", ["employees", "accreditation"], {"missing_fields": ["accreditation"]}
    )
    assert queries[:2] == ['"accreditation"', '"employees"']
