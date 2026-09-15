"""Real-document, live-model evidence-recovery probe for three fixed public RFC seeds.

Measures whether the unmodified Engine/Reasoner/select_passages pipeline recovers 18
hand-labeled scalar facts (6 per document; see tests/fixtures/real_documents_manifest.json)
from unmodified public RFC text, at reading_passes=1 and reading_passes=3. Reports honest
per-label exposure (was the supporting quote ever shown to the model) versus recovery (did
the model return the correct value backed by a verifiably located quote); job completion
never implies a correct answer. No network or model call happens unless --run-live is
passed; --report-only re-analyzes the exact job IDs of a prior --run-live report with zero
new network/model calls, and refuses (unless told otherwise) if the manifest has since
changed. --run-live durably persists its report to --output after every completed job, so
an interrupted run resumes without re-running already-completed work or losing its
originally measured latency.

Known scoring limitation: value comparison is strict-scalar (see `_normalize`) against a
compact alias list. A semantically correct but differently phrased or typed answer (for
example the word "advisory" for the boolean status_advisory field) is reported as incorrect
rather than guessed at -- this is a measurement floor, not a defect to silently patch over.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from harvest.config import Settings
from harvest.engine import Engine
from harvest.models import JobSpec, Limits
from harvest.store import Store, digest

DEFAULT_MANIFEST = (
    Path(__file__).resolve().parent.parent / "tests/fixtures/real_documents_manifest.json"
)
READING_PASSES_DEFAULT = (1, 3)
JOB_SECONDS = 180
JOB_MODEL_CALLS = 3
JOB_ATTEMPTS = 1


def atomic_write_text(path, text):
    """Replace `path`'s contents without ever leaving a truncated file in its place.

    Path.write_text opens the destination and truncates it before writing; a crash or
    exception mid-write leaves a corrupt or empty file where the last valid checkpoint used
    to be. Writing to a sibling temp file first (same directory, so the final os.replace is
    on one filesystem and therefore atomic) and swapping it in means the destination is
    always either the old, fully-written content or the new, fully-written content.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_manifest(path=DEFAULT_MANIFEST):
    documents = json.loads(Path(path).read_text())["documents"]
    for doc in documents:
        fields = [label["field"] for label in doc["labels"]]
        if len(fields) != len(set(fields)):
            raise ValueError(f"rfc {doc['rfc']}: duplicate field labels")
        for label in doc["labels"]:
            if label["expected_value"] not in label["value_aliases"]:
                raise ValueError(
                    f"rfc {doc['rfc']}/{label['field']}: expected_value missing from value_aliases"
                )
    return documents


# ---------------------------------------------------------------------------
# Scoring: pure functions over plain data, independent of Engine/Store, so the
# RED/GREEN behaviors below are unit-testable without any network or model.
# ---------------------------------------------------------------------------

_UNPARSEABLE = object()
_TRUE_WORDS = {"true", "yes", "y"}
_FALSE_WORDS = {"false", "no", "n"}


def _normalize(value, value_type):
    """Type-strict scalar normalization. Bool and int never coerce into each other."""
    if value_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            word = value.strip().casefold()
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
        return _UNPARSEABLE
    if value_type == "integer":
        if isinstance(value, bool):
            return _UNPARSEABLE
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return _UNPARSEABLE
        return _UNPARSEABLE
    if value_type == "string":
        if isinstance(value, str):
            return value.strip().casefold()
        return _UNPARSEABLE
    raise ValueError(f"unknown value_type: {value_type!r}")


def value_matches(observed_value, label):
    normalized = _normalize(observed_value, label["value_type"])
    if normalized is _UNPARSEABLE:
        return False
    aliases = {_normalize(alias, label["value_type"]) for alias in label["value_aliases"]}
    aliases.discard(_UNPARSEABLE)
    return normalized in aliases


def locator_offset(locator):
    if not isinstance(locator, str) or not locator.startswith("text:"):
        return None
    try:
        return int(locator[len("text:") :])
    except ValueError:
        return None


def locator_valid(observation, original_text):
    """Independently re-verify the claimed quote is really at the claimed offset.

    Reasoner.decide already rejects quotes that are not an exact contiguous substring of a
    shown passage before persisting a claim; this re-checks the persisted observation itself
    as a defense-in-depth audit, not a trust of that earlier, in-process check.
    """
    offset = locator_offset(observation.get("locator", ""))
    evidence = observation.get("evidence", "")
    if offset is None or offset < 0 or not evidence:
        return False
    return original_text[offset : offset + len(evidence)] == evidence


def quote_span(quote, text):
    index = text.find(quote)
    return None if index < 0 else (index, index + len(quote))


def is_exposed(label, text, exposure_spans):
    """Was any expected supporting quote fully contained in a passage shown to the model."""
    for quote in label["expected_quotes"]:
        span = quote_span(quote, text)
        if span and any(span[0] >= start and span[1] <= end for start, end in exposure_spans):
            return True, quote
    return False, None


def score_label(label, *, text, exposure_spans, observations):
    """Score one label against every observation sharing its field name.

    Every candidate observation is retained on the result (`candidate_observations`), not
    only a winner, so wrong or rejected extractions stay visible instead of being
    cherry-picked out of the report.
    """
    exposed, exposed_quote = is_exposed(label, text, exposure_spans)
    candidates = [o for o in observations if o["field"] == label["field"]]
    correct = next(
        (o for o in candidates if value_matches(o["value"], label) and locator_valid(o, text)),
        None,
    )
    if correct is not None:
        status = "correct"
    elif candidates:
        status = "incorrect"
    else:
        status = "absent"
    return {
        "field": label["field"],
        "expected_value": label["expected_value"],
        "exposed": exposed,
        "exposed_quote": exposed_quote,
        "recovered": correct is not None,
        "status": status,
        "correct_observation": correct,
        "candidate_observations": candidates,
    }


# ---------------------------------------------------------------------------
# Job construction and execution against the unmodified Engine.
# ---------------------------------------------------------------------------


def build_objective(doc):
    lines = [
        doc["objective"],
        "",
        "Fields to answer, each as a short scalar value with a supporting literal quote:",
    ]
    lines += [f"- {label['field']}: {label['question']}" for label in doc["labels"]]
    return "\n".join(lines)


def job_spec_for(doc, reading_passes):
    return JobSpec(
        objective=build_objective(doc),
        seeds=[doc["url"]],
        fields=[label["field"] for label in doc["labels"]],
        allowed_domains=[urlsplit(doc["url"]).hostname],
        use_model=True,
        mode="targeted",
        limits=Limits(
            depth=0,
            attempts=JOB_ATTEMPTS,
            seconds=JOB_SECONDS,
            model_calls=JOB_MODEL_CALLS,
            reading_passes=reading_passes,
        ),
    )


def manifest_fingerprint(doc):
    """Content hash of the entire manifest document (labels, aliases, quotes, corpus hash).

    Broader than what JobSpec itself hashes: expected_value/value_aliases/expected_quotes
    are deliberately never sent to the model, so editing only ground truth would not change
    JobSpec's own spec_hash. This fingerprint still must change when they do, so a run-live
    resume or a report-only rescore can detect that the manifest has drifted.
    """
    return digest(json.dumps(doc, sort_keys=True, default=str))


def job_key(doc, reading_passes, settings):
    """Fold reading_passes, model identity and the manifest itself into the idempotency key.

    A rerun with an unchanged configuration must reuse the same job (no new GET/model
    calls, via Store.create's own idempotency-key dedup). A changed reading_passes, model
    identity (including model_output_tokens, which affects reserved cost/output size but is
    not part of JobSpec) or manifest content must land on a different job rather than
    silently reusing an old report; only a genuine spec drift under an unchanged key should
    surface as Store's explicit error.
    """
    fingerprint = digest(
        json.dumps(
            {
                "reading_passes": reading_passes,
                "model_url": settings.model_url,
                "model_name": settings.model_name,
                "model_usd_per_million": settings.model_usd_per_million,
                "model_output_tokens": settings.model_output_tokens,
                "manifest_fingerprint": manifest_fingerprint(doc),
            },
            sort_keys=True,
        )
    )
    return f"benchmark-real-documents:v1:{fingerprint}"


def run_document(engine, doc, reading_passes, key):
    job_id = engine.submit(job_spec_for(doc, reading_passes), key)
    started = time.perf_counter()
    job = engine.run(job_id)
    return job_id, job, time.perf_counter() - started


# ---------------------------------------------------------------------------
# Reporting: reads back through the public Store API only, so it works
# identically for a fresh live run and for --report-only over an old database.
# ---------------------------------------------------------------------------


def _placeholder_labels(doc, status):
    """Non-scored label rows with the same shape score_label produces.

    Used whenever a row cannot be scored (hash mismatch, manifest drift, missing job) so the
    label count for that document never shrinks the denominator in build_report's summary.
    """
    return [
        {
            "field": label["field"],
            "expected_value": label["expected_value"],
            "exposed": None,
            "exposed_quote": None,
            "recovered": False,
            "status": status,
            "correct_observation": None,
            "candidate_observations": [],
        }
        for label in doc["labels"]
    ]


_OUTCOME_EVENT_TYPES = ("task_done", "failed", "deferred", "lease_expired")
_FAILURE_EVENT_TYPES = (
    "failed",
    "deferred",
    "lease_expired",
    "blocked",
    "budget_exhausted",
    "cancelled",
)


def analyze_job(store, job_id, doc):
    job = store.job(job_id)
    source_captures = [c for c in store.captures(job_id) if c["url"] == doc["url"]]
    capture = store.capture(source_captures[0]["id"]) if source_captures else None
    actual_body_sha256 = capture["body_hash"] if capture else None
    comparable = actual_body_sha256 == doc["expected_body_sha256"]
    text = capture["body"].decode("utf-8", errors="replace") if capture else ""

    events = store.events(job_id, limit=10000)
    reason_details = [
        e["details"] for e in events if e["type"] == "task_done" and "selection" in e["details"]
    ]
    exposure_spans = [
        (span["start"], span["end"])
        for details in reason_details
        for span in details["selection"]["spans"]
    ]
    model_reserved = [e["details"] for e in events if e["type"] == "model_reserved"]
    model_names_used = sorted({d["model"] for d in model_reserved})

    # Attempted-but-failed model calls still reserve (and audit) before the network call, so
    # a task's outcome events -- success or failure -- are joined back onto its reservation.
    outcomes_by_task = {}
    for event in events:
        details = event["details"]
        if event["type"] in _OUTCOME_EVENT_TYPES and "task" in details:
            outcomes_by_task.setdefault(details["task"], []).append(
                {
                    "type": event["type"],
                    **{k: v for k, v in details.items() if k not in ("selection", "decision")},
                }
            )
    model_calls_audit = [
        {
            "task": details.get("task"),
            "attempt": details.get("attempt"),
            "reading_pass": details.get("reading_pass"),
            "prompt_sha256": details.get("prompt_sha256"),
            "capture_id": details.get("capture_id"),
            "selection_spans": details.get("selection", {}).get("spans", []),
            "excluded_span_count": details.get("excluded_span_count"),
            "outcomes": outcomes_by_task.get(details.get("task"), []),
        }
        for details in model_reserved
    ]
    # Proposed claims include ones Reasoner.decide rejected as unsupported quotes; those never
    # reach `observations`, so this is the only place a rejected claim stays visible at all.
    decision_audit = [
        {
            "reading_pass": details["reading_pass"],
            "rejected_unsupported_quotes": details.get("rejected_unsupported_quotes", 0),
            "proposed_claims": details["decision"]["claims"],
            "gaps": details["decision"].get("gaps", []),
            "contradictions": details["decision"].get("contradictions", []),
            "rationale": details["decision"].get("rationale", ""),
        }
        for details in reason_details
        if "decision" in details
    ]
    failed_events = [
        {"type": event["type"], "details": event["details"]}
        for event in events
        if event["type"] in _FAILURE_EVENT_TYPES
    ]

    observations = store.observations(job_id, limit=1000) if comparable else []
    labels = (
        [
            score_label(label, text=text, exposure_spans=exposure_spans, observations=observations)
            for label in doc["labels"]
        ]
        if comparable
        else _placeholder_labels(doc, "not_comparable")
    )
    return {
        "rfc": doc["rfc"],
        "source_url": doc["url"],
        "job_id": job_id,
        "job_status": job["status"],
        "missing_fields": job["missing_fields"],
        "requests": job["requests"],
        "source_get_count": len(source_captures),
        "capture_id": capture["id"] if capture else None,
        "model_calls": job["model_calls"],
        "model_names_used": model_names_used,
        "reading_passes_performed": sorted({d["reading_pass"] for d in reason_details}),
        "rejected_unsupported_quotes": sum(
            d.get("rejected_unsupported_quotes", 0) for d in reason_details
        ),
        "provider_usage": [d["provider_usage"] for d in reason_details if "provider_usage" in d],
        "exposure_spans": [list(span) for span in exposure_spans],
        "model_calls_audit": model_calls_audit,
        "decision_audit": decision_audit,
        "failed_events": failed_events,
        "comparable": comparable,
        "expected_body_sha256": doc["expected_body_sha256"],
        "actual_body_sha256": actual_body_sha256,
        "normalized_text_sha256": digest(text) if text else None,
        "normalized_chars": len(text),
        "all_observations": observations,
        "labels": labels,
    }


def build_report(runs, reading_passes_list, configured_model=None):
    summary = {}
    for reading_passes in reading_passes_list:
        subset = [r for r in runs if r["reading_passes_setting"] == reading_passes]
        labels = [label for r in subset for label in r["labels"]]
        by_status = {}
        for label in labels:
            by_status[label["status"]] = by_status.get(label["status"], 0) + 1
        summary[str(reading_passes)] = {
            "documents": len(subset),
            "labels_total": len(labels),
            "labels_by_status": by_status,
            "labels_correct": by_status.get("correct", 0),
            "labels_incorrect": by_status.get("incorrect", 0),
            "labels_absent": by_status.get("absent", 0),
            "labels_not_comparable": by_status.get("not_comparable", 0),
            "labels_exposed": sum(1 for label in labels if label["exposed"]),
            "labels_exposed_not_recovered": sum(
                1 for label in labels if label["exposed"] and not label["recovered"]
            ),
        }
    return {
        "harness": "scripts/benchmark_real_documents.py",
        "configured_model": configured_model,
        "reading_passes_settings": list(reading_passes_list),
        "runs": runs,
        "summary": summary,
    }


def _configured_model(settings):
    return {
        "url": settings.model_url,
        "name": settings.model_name,
        "usd_per_million": settings.model_usd_per_million,
        "output_tokens": settings.model_output_tokens,
    }


def load_partial_report(output_path):
    """Read a durable report previously written by run_benchmark's `on_row` callback.

    Returns rows keyed by (rfc, reading_passes_setting) for resume lookups. A genuinely
    absent file means "nothing completed yet" (the first run of a new --output path) and
    returns an empty cache. An *existing* file that fails to read or parse is a different
    situation entirely -- most likely a checkpoint left mid-write by an unrelated crash --
    and must fail loudly rather than being silently treated as empty, which would discard
    expensive already-measured timing evidence instead of surfacing the corruption.
    """
    path = Path(output_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"existing checkpoint {path} is unreadable or corrupt ({exc}); "
            "resolve or remove it explicitly before resuming -- it is not treated as absent"
        ) from exc
    return {
        (row["rfc"], row["reading_passes_setting"]): row
        for row in data.get("runs", [])
        if "idempotency_key" in row
    }


def _cached_row_usable(store, cached, expected_key):
    """A previously persisted row is reusable only if its exact config still matches and its
    job still exists. expected_key already folds in reading_passes/model identity/manifest
    content (job_key), so equality here is sufficient to detect any of those having changed.
    """
    if not cached or cached.get("idempotency_key") != expected_key or not cached.get("job_id"):
        return False
    try:
        store.job(cached["job_id"])
    except KeyError:
        return False
    return True


def run_benchmark(
    engine,
    documents,
    reading_passes_list=READING_PASSES_DEFAULT,
    *,
    previous_runs=None,
    on_row=None,
):
    """Explicit-opt-in live path: submits/runs seeded jobs, then reports via analyze_job.

    `previous_runs` (as returned by load_partial_report) lets a resumed invocation skip
    re-running a row whose config and manifest are unchanged, reusing that row verbatim --
    including its originally measured `latency_seconds`, which an idempotent no-op rerun
    through Engine.run would otherwise silently replace with a near-zero value. `on_row`,
    if given, is called with the full report accumulated so far after every row, so a crash
    mid-run loses nothing that already completed.
    """
    previous_runs = previous_runs or {}
    runs = []
    for doc in documents:
        for reading_passes in reading_passes_list:
            key = job_key(doc, reading_passes, engine.settings)
            cached = previous_runs.get((doc["rfc"], reading_passes))
            if _cached_row_usable(engine.store, cached, key):
                runs.append(cached)
            else:
                job_id, _job, latency_seconds = run_document(engine, doc, reading_passes, key)
                report = analyze_job(engine.store, job_id, doc)
                report["reading_passes_setting"] = reading_passes
                report["latency_seconds"] = latency_seconds
                report["idempotency_key"] = key
                report["manifest_fingerprint"] = manifest_fingerprint(doc)
                runs.append(report)
            if on_row:
                on_row(
                    build_report(
                        list(runs), reading_passes_list, _configured_model(engine.settings)
                    )
                )
    return build_report(runs, reading_passes_list, _configured_model(engine.settings))


def rescore(store, documents, input_report, *, allow_manifest_drift=False):
    """No-network, no-model rescoring bound to the exact job IDs of a prior --run-live report.

    Never scans the database for "the latest matching job": an unrelated Harvest job, or a
    job produced under a different model configuration, can share the same seed URL and
    reading_passes, and silently picking one would misattribute its observations to this
    benchmark. Each row is instead re-analyzed by the exact job_id recorded in input_report.
    A manifest that changed since that report was produced is refused (rows marked
    "manifest_drift_refused") unless `allow_manifest_drift=True`, in which case rescoring
    proceeds but every affected row is flagged with `manifest_drift: True` rather than
    silently applying new ground truth to an old job's observations unremarked.
    """
    documents_by_rfc = {doc["rfc"]: doc for doc in documents}
    runs = []
    for prior in input_report.get("runs", []):
        rfc = prior.get("rfc")
        reading_passes = prior.get("reading_passes_setting")
        job_id = prior.get("job_id")
        doc = documents_by_rfc.get(rfc)
        if doc is None:
            runs.append(
                {
                    "rfc": rfc,
                    "source_url": prior.get("source_url"),
                    "reading_passes_setting": reading_passes,
                    "job_id": job_id,
                    "comparable": False,
                    "labels": [
                        {**label, "status": "manifest_missing_document"}
                        for label in prior.get("labels", [])
                    ],
                    "status": "manifest_missing_document",
                }
            )
            continue
        current_fingerprint = manifest_fingerprint(doc)
        drift = prior.get("manifest_fingerprint") != current_fingerprint
        if drift and not allow_manifest_drift:
            runs.append(
                {
                    "rfc": doc["rfc"],
                    "source_url": doc["url"],
                    "reading_passes_setting": reading_passes,
                    "job_id": job_id,
                    "comparable": False,
                    "labels": _placeholder_labels(doc, "manifest_drift_refused"),
                    "status": "manifest_drift_refused",
                    "manifest_drift": True,
                }
            )
            continue
        if not job_id:
            runs.append(
                {
                    "rfc": doc["rfc"],
                    "source_url": doc["url"],
                    "reading_passes_setting": reading_passes,
                    "job_id": None,
                    "comparable": False,
                    "labels": _placeholder_labels(doc, "no_job_id_in_input_report"),
                    "status": "no_job_id_in_input_report",
                }
            )
            continue
        try:
            report = analyze_job(store, job_id, doc)
        except KeyError:
            runs.append(
                {
                    "rfc": doc["rfc"],
                    "source_url": doc["url"],
                    "reading_passes_setting": reading_passes,
                    "job_id": job_id,
                    "comparable": False,
                    "labels": _placeholder_labels(doc, "job_not_found_in_database"),
                    "status": "job_not_found_in_database",
                }
            )
            continue
        report["reading_passes_setting"] = reading_passes
        report["manifest_fingerprint"] = current_fingerprint
        report["manifest_drift"] = drift
        runs.append(report)
    reading_passes_list = sorted(
        {r["reading_passes_setting"] for r in runs if r["reading_passes_setting"] is not None}
    )
    result = build_report(runs, reading_passes_list, configured_model=None)
    result["mode"] = "report-only"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--db", help="SQLite database path (required with --run-live or --report-only)"
    )
    parser.add_argument(
        "--output",
        help="Report path (default: stdout). Required with --run-live: doubles as the "
        "durable resume checkpoint, read at startup and rewritten after every completed job",
    )
    parser.add_argument(
        "--input",
        help="A prior --output report to rescore (required with --report-only)",
    )
    parser.add_argument(
        "--manifest", default=str(DEFAULT_MANIFEST), help="Path to the labeled manifest JSON"
    )
    parser.add_argument(
        "--reading-passes",
        default="1,3",
        help="Comma-separated reading_passes settings to run (default: 1,3; --report-only "
        "always uses the reading_passes recorded in --input instead)",
    )
    parser.add_argument(
        "--run-live",
        action="store_true",
        help="Opt-in: perform real HTTP fetches and real model calls",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Rescore the exact jobs named in --input with no network or model calls",
    )
    parser.add_argument(
        "--allow-manifest-drift",
        action="store_true",
        help="With --report-only, rescore even if the manifest changed since --input was "
        "produced, instead of refusing (each affected row is still flagged manifest_drift)",
    )
    args = parser.parse_args(argv)

    if args.run_live and args.report_only:
        parser.error("--run-live and --report-only are mutually exclusive")
    try:
        reading_passes_list = tuple(int(x) for x in args.reading_passes.split(","))
    except ValueError:
        parser.error("--reading-passes must be a comma-separated list of integers")

    documents = load_manifest(args.manifest)

    if not args.run_live and not args.report_only:
        print(
            f"Loaded manifest: {len(documents)} documents, "
            f"{sum(len(doc['labels']) for doc in documents)} labels. "
            "No network or model calls made. Pass --run-live --output PATH (real "
            "network+model) or --report-only --db PATH --input PATH (rescore a prior "
            "report's exact jobs) to execute."
        )
        return 0

    if not args.db:
        parser.error("--db is required with --run-live or --report-only")

    if args.report_only:
        if not args.input:
            parser.error("--input (a prior --output report) is required with --report-only")
        input_report = json.loads(Path(args.input).read_text())
        report = rescore(
            Store(args.db), documents, input_report, allow_manifest_drift=args.allow_manifest_drift
        )
    else:
        if not args.output:
            parser.error("--output is required with --run-live for durable incremental reporting")
        engine = Engine(Settings(database=args.db))
        previous_runs = load_partial_report(args.output)

        def persist(partial_report):
            atomic_write_text(args.output, json.dumps(partial_report, indent=2, default=str))

        report = run_benchmark(
            engine, documents, reading_passes_list, previous_runs=previous_runs, on_row=persist
        )

    output = json.dumps(report, indent=2, default=str)
    if args.output:
        atomic_write_text(args.output, output)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
