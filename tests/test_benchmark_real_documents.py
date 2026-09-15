"""Offline coverage for scripts/benchmark_real_documents.py: scorer RED/GREEN behaviors,
hash-mismatch handling, safe opt-in, and idempotent/report-only execution. No test in this
file performs a real network request or a real model call.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from harvest.store import Store, digest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "benchmark_real_documents.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("benchmark_real_documents", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bm = _load_script()

STRING_LABEL = {
    "field": "media_type",
    "question": "What is the media type?",
    "expected_value": "application/json",
    "value_aliases": ["application/json"],
    "value_type": "string",
    "expected_quotes": ["media type for JSON text is application/json"],
}

INTEGER_LABEL = {
    "field": "cache_max_hours",
    "question": "How many hours?",
    "expected_value": 24,
    "value_aliases": [24, "24"],
    "value_type": "integer",
    "expected_quotes": ["cached version for more than 24 hours"],
}

BOOLEAN_LABEL = {
    "field": "status_advisory",
    "question": "Is it advisory?",
    "expected_value": True,
    "value_aliases": [True, "true"],
    "value_type": "boolean",
    "expected_quotes": ["is only advisory"],
}

TEXT = (
    "Intro filler. The media type for JSON text is application/json. More filler text here. "
    "Consumers SHOULD NOT use the cached version for more than 24 hours, unless offline. "
    "The status member, if present, is only advisory; trailing filler follows to pad it out."
)


def observation(field, value, evidence, offset, locator=None):
    return {
        "field": field,
        "value": value,
        "evidence": evidence,
        "locator": locator if locator is not None else f"text:{offset}",
    }


# ---------------------------------------------------------------------------
# Manifest structure
# ---------------------------------------------------------------------------


def test_manifest_has_three_documents_and_eighteen_labels():
    documents = bm.load_manifest()
    assert len(documents) == 3
    labels = [label for doc in documents for label in doc["labels"]]
    assert len(labels) == 18
    for doc in documents:
        fields = [label["field"] for label in doc["labels"]]
        assert len(fields) == len(set(fields)) == 6
    for label in labels:
        assert label["expected_value"] in label["value_aliases"]
        assert label["expected_quotes"]
        assert label["value_type"] in ("string", "integer", "boolean")


def test_load_manifest_rejects_duplicate_field_labels(tmp_path):
    bad = {
        "documents": [
            {
                "rfc": 1,
                "url": "https://example.org/doc",
                "expected_body_sha256": "0" * 64,
                "objective": "x",
                "labels": [
                    {**STRING_LABEL, "field": "dup"},
                    {**INTEGER_LABEL, "field": "dup"},
                ],
            }
        ]
    }
    path = tmp_path / "bad_manifest.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="duplicate field labels"):
        bm.load_manifest(path)


def test_load_manifest_rejects_expected_value_missing_from_aliases(tmp_path):
    bad = {
        "documents": [
            {
                "rfc": 1,
                "url": "https://example.org/doc",
                "expected_body_sha256": "0" * 64,
                "objective": "x",
                "labels": [{**STRING_LABEL, "value_aliases": ["something else"]}],
            }
        ]
    }
    path = tmp_path / "bad_manifest.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="expected_value missing"):
        bm.load_manifest(path)


# ---------------------------------------------------------------------------
# Scorer: RED -> GREEN behaviors
# ---------------------------------------------------------------------------


def test_correct_value_with_valid_locator_recovers():
    offset = TEXT.index("media type for JSON text is application/json")
    obs = [
        observation(
            "media_type", "application/json", "media type for JSON text is application/json", offset
        )
    ]
    result = bm.score_label(
        STRING_LABEL, text=TEXT, exposure_spans=[(0, len(TEXT))], observations=obs
    )
    assert result["status"] == "correct"
    assert result["recovered"] is True
    assert result["exposed"] is True


def test_right_field_wrong_value_fails():
    offset = TEXT.index("media type for JSON text is application/json")
    obs = [
        observation(
            "media_type", "application/xml", "media type for JSON text is application/json", offset
        )
    ]
    result = bm.score_label(
        STRING_LABEL, text=TEXT, exposure_spans=[(0, len(TEXT))], observations=obs
    )
    assert result["status"] == "incorrect"
    assert result["recovered"] is False


def test_exact_quote_wrong_locator_fails():
    real_quote = "media type for JSON text is application/json"
    real_offset = TEXT.index(real_quote)
    wrong_offset = 0  # a real, exact quote, but claimed at an offset it does not occupy
    assert TEXT[wrong_offset : wrong_offset + len(real_quote)] != real_quote
    obs = [
        observation(
            "media_type",
            "application/json",
            real_quote,
            real_offset,
            locator=f"text:{wrong_offset}",
        )
    ]
    result = bm.score_label(
        STRING_LABEL, text=TEXT, exposure_spans=[(0, len(TEXT))], observations=obs
    )
    assert result["status"] == "incorrect"
    assert result["recovered"] is False


def test_absent_label_when_no_observation_and_not_exposed():
    result = bm.score_label(INTEGER_LABEL, text=TEXT, exposure_spans=[], observations=[])
    assert result["status"] == "absent"
    assert result["recovered"] is False
    assert result["exposed"] is False


def test_exposed_but_not_recovered_when_quote_shown_but_no_claim_made():
    quote = INTEGER_LABEL["expected_quotes"][0]
    span = bm.quote_span(quote, TEXT)
    result = bm.score_label(INTEGER_LABEL, text=TEXT, exposure_spans=[span], observations=[])
    assert result["exposed"] is True
    assert result["recovered"] is False
    assert result["status"] == "absent"


def test_boolean_true_does_not_match_integer_one():
    obs = [observation("status_advisory", 1, "is only advisory", TEXT.index("is only advisory"))]
    result = bm.score_label(
        BOOLEAN_LABEL, text=TEXT, exposure_spans=[(0, len(TEXT))], observations=obs
    )
    assert result["status"] == "incorrect"
    assert result["recovered"] is False


def test_integer_field_does_not_match_boolean_true():
    obs = [
        observation(
            "cache_max_hours", True, "for more than 24 hours", TEXT.index("for more than 24 hours")
        )
    ]
    result = bm.score_label(
        INTEGER_LABEL, text=TEXT, exposure_spans=[(0, len(TEXT))], observations=obs
    )
    assert result["status"] == "incorrect"
    assert result["recovered"] is False


def test_string_field_rejects_numeric_and_boolean_observed_values():
    assert bm.value_matches(1, STRING_LABEL) is False
    assert bm.value_matches(True, STRING_LABEL) is False


def test_value_matches_accepts_declared_aliases():
    assert bm.value_matches("24", INTEGER_LABEL) is True
    assert bm.value_matches(24.0, INTEGER_LABEL) is True
    assert bm.value_matches(25, INTEGER_LABEL) is False


# ---------------------------------------------------------------------------
# Full pipeline against a local fixture server: hash mismatch, idempotency,
# reading_passes change, and report-only rescoring. No real network/model.
# ---------------------------------------------------------------------------


def fixture_doc(source):
    body = (
        "Intro filler text before the facts. alpha value is one. Some separating filler "
        "text goes here. beta value is 2. Trailing filler text follows after the facts."
    )
    source["routes"] = {"/fixture": (body, "text/plain")}

    def model_reader(request):
        prompt = json.loads(request["messages"][1]["content"])
        text = prompt["SOURCE_TEXT"]
        claims = []
        if "alpha value is one" in text:
            claims.append(
                {"field": "alpha", "value": "one", "quote": "alpha value is one", "confidence": 0.9}
            )
        if "beta value is 2" in text:
            claims.append(
                {"field": "beta", "value": 2, "quote": "beta value is 2", "confidence": 0.9}
            )
        return {"claims": claims, "rationale": "fixture"}

    source["model_reader"] = model_reader
    doc = {
        "rfc": 0,
        "title": "Fixture",
        "url": source["base"] + "/fixture",
        "expected_body_sha256": digest(body.encode()),
        "objective": "Extract fixture facts.",
        "labels": [
            {
                "field": "alpha",
                "question": "What is alpha?",
                "expected_value": "one",
                "value_aliases": ["one"],
                "value_type": "string",
                "expected_quotes": ["alpha value is one"],
            },
            {
                "field": "beta",
                "question": "What is beta?",
                "expected_value": 2,
                "value_aliases": [2, "2"],
                "value_type": "integer",
                "expected_quotes": ["beta value is 2"],
            },
        ],
    }
    return doc, body


def configure_model(engine, source):
    engine.settings.model_url = source["base"] + "/v1"
    engine.settings.model_name = "fixture-model"
    engine.settings.model_usd_per_million = 0


def test_run_benchmark_recovers_fixture_labels_and_report_has_required_fields(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    report = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))
    run = report["runs"][0]
    assert run["job_status"] == "completed"
    assert run["comparable"] is True
    assert run["source_get_count"] == 1
    assert run["source_url"] == doc["url"]
    assert run["expected_body_sha256"] == doc["expected_body_sha256"]
    assert run["actual_body_sha256"] == doc["expected_body_sha256"]
    statuses = {label["field"]: label["status"] for label in run["labels"]}
    assert statuses == {"alpha": "correct", "beta": "correct"}
    assert report["summary"]["1"]["labels_correct"] == 2


def test_idempotent_rerun_issues_no_new_get_or_model_calls(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    first = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))["runs"][0]
    get_count = source["counts"]["/fixture"]
    model_calls = source["model_calls"]

    second = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))["runs"][0]
    assert second["job_id"] == first["job_id"]
    assert source["counts"]["/fixture"] == get_count
    assert source["model_calls"] == model_calls


def test_changed_reading_passes_creates_a_new_job_not_a_silent_reuse(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    passes_one = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))["runs"][0]
    passes_three = bm.run_benchmark(engine, [doc], reading_passes_list=(3,))["runs"][0]
    assert passes_one["job_id"] != passes_three["job_id"]


def test_job_key_changes_when_model_output_tokens_or_manifest_content_changes(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    base_key = bm.job_key(doc, 1, engine.settings)

    engine.settings.model_output_tokens = engine.settings.model_output_tokens + 1
    assert bm.job_key(doc, 1, engine.settings) != base_key
    engine.settings.model_output_tokens -= 1
    assert bm.job_key(doc, 1, engine.settings) == base_key

    edited_doc = json.loads(json.dumps(doc))
    edited_doc["labels"][0]["question"] = "A rephrased question."
    assert bm.job_key(edited_doc, 1, engine.settings) != base_key


def test_report_includes_model_calls_audit_and_decision_audit_without_full_prompts(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    run = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))["runs"][0]

    assert len(run["model_calls_audit"]) == 1
    call = run["model_calls_audit"][0]
    assert call["reading_pass"] == 1
    assert call["prompt_sha256"]
    assert call["selection_spans"]
    assert any(o["type"] == "task_done" for o in call["outcomes"])
    for outcome in call["outcomes"]:
        assert "selection" not in outcome and "decision" not in outcome

    assert len(run["decision_audit"]) == 1
    decision = run["decision_audit"][0]
    assert decision["reading_pass"] == 1
    assert {c["field"] for c in decision["proposed_claims"]} == {"alpha", "beta"}
    assert run["exposure_spans"]
    assert run["failed_events"] == []

    dumped = json.dumps(run)
    assert "SOURCE_TEXT" not in dumped  # full prompts are never embedded in the report


def test_run_live_resume_after_injected_failure_preserves_latency_and_avoids_rerun(
    engine, source, tmp_path, monkeypatch
):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    output_path = tmp_path / "report.json"

    real_run_document = bm.run_document
    call_count = {"n": 0}

    def flaky_run_document(engine, doc, reading_passes, key):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("injected failure after the first completed job")
        return real_run_document(engine, doc, reading_passes, key)

    monkeypatch.setattr(bm, "run_document", flaky_run_document)

    def persist(partial_report):
        bm.atomic_write_text(output_path, json.dumps(partial_report, indent=2, default=str))

    with pytest.raises(RuntimeError, match="injected failure"):
        bm.run_benchmark(
            engine, [doc], reading_passes_list=(1, 3), previous_runs={}, on_row=persist
        )

    # The first row's durable write survived the crash on the second row.
    partial = json.loads(output_path.read_text())
    assert len(partial["runs"]) == 1
    first_row = partial["runs"][0]
    assert first_row["reading_passes_setting"] == 1
    assert first_row["job_status"] == "completed"
    get_count_after_crash = source["counts"]["/fixture"]
    model_calls_after_crash = source["model_calls"]

    monkeypatch.setattr(bm, "run_document", real_run_document)
    previous_runs = bm.load_partial_report(output_path)
    resumed = bm.run_benchmark(
        engine, [doc], reading_passes_list=(1, 3), previous_runs=previous_runs, on_row=persist
    )

    resumed_first, resumed_second = resumed["runs"]
    # Row 1 (cached) is reused byte-for-byte, proving it was not rerun: an idempotent no-op
    # rerun through Engine.run would complete almost instantly, not reproduce this exact
    # originally measured latency.
    assert resumed_first["job_id"] == first_row["job_id"]
    assert resumed_first["latency_seconds"] == first_row["latency_seconds"]
    assert resumed_first == first_row
    # Row 2 (reading_passes=3) is a genuinely new job: exactly one more GET/model call each,
    # attributable to row 2 alone since row 1 made none.
    assert source["counts"]["/fixture"] == get_count_after_crash + 1
    assert source["model_calls"] == model_calls_after_crash + 1
    assert resumed_second["reading_passes_setting"] == 3
    assert resumed_second["job_status"] == "completed"
    assert resumed_second["job_id"] != resumed_first["job_id"]


# ---------------------------------------------------------------------------
# Durability: atomic checkpoint writes and explicit-failure checkpoint reads.
# ---------------------------------------------------------------------------


def test_atomic_write_failure_preserves_previous_valid_checkpoint(tmp_path, monkeypatch):
    output_path = tmp_path / "report.json"
    original = json.dumps({"runs": [{"rfc": 9309, "reading_passes_setting": 1}]})
    output_path.write_text(original)

    def boom(*_args, **_kwargs):
        raise OSError("simulated failure between temp-file write and replace")

    monkeypatch.setattr(bm.os, "replace", boom)
    with pytest.raises(OSError, match="simulated failure"):
        bm.atomic_write_text(output_path, json.dumps({"runs": []}))

    # The previous checkpoint is untouched, and no leftover temp file was left behind.
    assert output_path.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == [output_path.name]


def test_atomic_write_replaces_content_on_success(tmp_path):
    output_path = tmp_path / "report.json"
    output_path.write_text("stale")
    bm.atomic_write_text(output_path, "fresh")
    assert output_path.read_text() == "fresh"
    assert [p.name for p in tmp_path.iterdir()] == [output_path.name]


def test_load_partial_report_returns_empty_for_a_genuinely_absent_file(tmp_path):
    assert bm.load_partial_report(tmp_path / "never-written.json") == {}


def test_load_partial_report_fails_explicitly_on_corrupt_existing_checkpoint(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    with pytest.raises(ValueError, match="unreadable or corrupt"):
        bm.load_partial_report(path)


def test_hash_mismatch_is_explicit_and_not_scored(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    run = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))["runs"][0]
    tampered = {**doc, "expected_body_sha256": "0" * 64}
    analysis = bm.analyze_job(engine.store, run["job_id"], tampered)
    assert analysis["comparable"] is False
    assert all(label["status"] == "not_comparable" for label in analysis["labels"])
    assert all(label["recovered"] is False for label in analysis["labels"])


def test_report_only_rescores_without_network_or_model_calls(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    live_report = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))
    live = live_report["runs"][0]
    get_count = source["counts"]["/fixture"]
    model_calls = source["model_calls"]

    store = Store(engine.settings.database)
    rescored = bm.rescore(store, [doc], live_report)["runs"][0]
    assert source["counts"]["/fixture"] == get_count
    assert source["model_calls"] == model_calls
    assert rescored["job_id"] == live["job_id"]
    assert rescored["labels"] == live["labels"]
    assert rescored["manifest_drift"] is False


def test_rescore_never_picks_an_unrelated_job_sharing_the_same_seed_and_passes(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    live_report = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))

    # An unrelated Harvest job against the exact same seed URL and reading_passes, created
    # after the benchmark job -- "pick the latest match" would wrongly select this one.
    from urllib.parse import urlsplit

    from harvest.models import JobSpec, Limits

    unrelated_spec = JobSpec(
        objective="Unrelated investigation of the same URL",
        seeds=[doc["url"]],
        fields=["alpha"],
        allowed_domains=[urlsplit(doc["url"]).hostname],
        limits=Limits(depth=0, reading_passes=1),
    )
    engine.run(engine.submit(unrelated_spec, "unrelated-job"))

    store = Store(engine.settings.database)
    rescored = bm.rescore(store, [doc], live_report)["runs"][0]
    assert rescored["job_id"] == live_report["runs"][0]["job_id"]


def test_rescore_requires_exact_job_id_reports_missing_job_without_shrinking_denominator(
    engine, source
):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    live_report = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))
    live_report["runs"][0]["job_id"] = "does-not-exist"

    store = Store(engine.settings.database)
    result = bm.rescore(store, [doc], live_report)
    run = result["runs"][0]
    assert run["status"] == "job_not_found_in_database"
    assert len(run["labels"]) == len(doc["labels"])
    assert result["summary"]["1"]["labels_total"] == len(doc["labels"])


def test_rescore_refuses_manifest_drift_unless_explicitly_allowed(engine, source):
    doc, _body = fixture_doc(source)
    configure_model(engine, source)
    live_report = bm.run_benchmark(engine, [doc], reading_passes_list=(1,))

    drifted_doc = json.loads(json.dumps(doc))
    drifted_doc["labels"][0]["question"] = "A different question entirely."

    store = Store(engine.settings.database)
    refused = bm.rescore(store, [drifted_doc], live_report)["runs"][0]
    assert refused["status"] == "manifest_drift_refused"
    assert refused["manifest_drift"] is True
    assert len(refused["labels"]) == len(doc["labels"])

    allowed = bm.rescore(store, [drifted_doc], live_report, allow_manifest_drift=True)["runs"][0]
    assert allowed["manifest_drift"] is True
    assert allowed["job_id"] == live_report["runs"][0]["job_id"]


# ---------------------------------------------------------------------------
# Safe opt-in: no --run-live/--report-only means no Engine/Store construction.
# ---------------------------------------------------------------------------


def test_main_default_constructs_no_engine_or_store(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise AssertionError("must not construct Engine/Store without --run-live/--report-only")

    monkeypatch.setattr(bm, "Engine", boom)
    monkeypatch.setattr(bm, "Store", boom)
    monkeypatch.chdir(tmp_path)
    assert bm.main([]) == 0
    assert "labels" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_main_requires_db_with_run_live(monkeypatch):
    monkeypatch.setattr(
        bm, "Engine", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no engine"))
    )
    with pytest.raises(SystemExit):
        bm.main(["--run-live"])


def test_main_rejects_run_live_and_report_only_together():
    with pytest.raises(SystemExit):
        bm.main(["--run-live", "--report-only", "--db", "unused.sqlite"])


def test_main_requires_output_with_run_live(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bm, "Engine", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no engine"))
    )
    with pytest.raises(SystemExit):
        bm.main(["--run-live", "--db", str(tmp_path / "x.sqlite")])


def test_main_requires_input_with_report_only(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bm, "Store", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no store"))
    )
    with pytest.raises(SystemExit):
        bm.main(["--report-only", "--db", str(tmp_path / "x.sqlite")])
