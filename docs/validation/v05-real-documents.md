# Real-document/live-model pilot and cross-job rereading fix

Base: `6469c86709a7aee7edb64d202407a94d4145230b`. This pilot exercised the real
Engine against official RFC 9309, 8259 and 9457 text, with six fixed scalar labels
per document. Source hashes and every reference quote were checked against captures.
The documents are 25,261–34,104 characters with varied real prose, but all are
technical standards: this is a diagnostic corpus, not representative research coverage.

The existing local `granite3.3:8b` model ran through Ollama's OpenAI-compatible
endpoint with temperature 0, 2,000 output tokens, no API key and zero configured
monetary cost. Model digest and raw result identifiers are in
[v05-real-documents.json](v05-real-documents.json).

## What the live run actually showed

| Baseline setting | Jobs completed / partial | Strict label matches | Model calls | Source captures |
|---|---:|---:|---:|---:|
| One reading pass | 2 / 1 | 1 / 18 | 3 | 3 |
| Up to three passes | 2 / 1 | 1 / 18 | 3 | 3 |

Do **not** interpret this as an unbiased comparison of reading strategies. The
shared-database experiment exposed a production defect: global observation deduplication
made a later job's first pass appear unproductive, suppressing its reread despite missing
fields. RFC8259's first job inserted two observations; the subsequent three-pass job
reused both and stopped with four fields missing.

`Store.finish` now uses new assertion membership in the current extraction to gate
rereading. All passes share that extraction; assertions known only to earlier jobs count
as new progress here, while repeated assertions within this extraction do not. Global
observation deduplication, `novel_observations`, and existing plateau accounting remain
unchanged. There is no schema, dependency, selector or quote-policy change.

A fresh RFC8259 job on a **copy retaining all baseline observations** verified the fix:
its first prompt hash is identical to the baseline, global novelty is zero, and it now
executes passes 1 and 2 using one source GET. The second pass's proposals were rejected;
strict recovery remains 1/6. This verifies the scheduling correction, not model quality.
The five unaffected baseline workloads were not rerun or represented as new executions.

## Model-quality failures retained, not hidden

- Each baseline setting produced five whitespace-only quote mismatches and five other
  quotes absent from the selected passages. Harvest rejected all ten. Model-generated
  absence claims and fabricated integer bounds were among the unsupported proposals.
- RFC9457 was partial after a schema-validation error. A separate diagnostic replay of
  its audited prompt reproduced missing `confidence` in all six proposed claims. This
  diagnoses the same failure class; the original invalid response was not persisted.
- A native JSON-schema control request, using the existing provider feature rather than
  a new library, was rejected with HTTP 400 (`failed to parse grammar`). A second request
  retained the error body after the first probe failed before writing its artifact.
- Strict alias scoring counts a valid paraphrase such as “Names within a JSON object
  SHOULD be unique.” as incorrect when it is not a declared alias. Scores are a conservative
  exact-value floor, not a semantic judge. No aliases were tuned after seeing results.
- Successful-call exposure covers 7/18 reference facts per baseline setting. The reserved
  first-pass inputs contain 9/18; RFC9457's failed model call explains the difference.
  Literal locator validity is not a proof that a quote entails the claimed value.

These results prioritize provider/schema compliance and faithful evidence quotation ahead
of a semantic retrieval subsystem. BEIR and MTEB were researched for broader retrieval
benchmarks; they do not replace this end-to-end field/provenance check. No embedding,
vector database, graph or evaluation-framework dependency was introduced.

## Reproduce and inspect

Run from the checkout. Network/model calls are opt-in; defaults only inspect the manifest.
Use an installed, verified model and explicit endpoint/price configuration. A paid endpoint
can incur cost; this recorded run used only the existing local model.

```sh
.venv/bin/python scripts/benchmark_real_documents.py
HARVEST_MODEL_URL=http://YOUR_LOCAL_HOST:11434/v1 \
HARVEST_MODEL_NAME=YOUR_INSTALLED_MODEL HARVEST_MODEL_USD_PER_MILLION=0 \
.venv/bin/python scripts/benchmark_real_documents.py --run-live \
  --db /absolute/path/evaluation.sqlite --output /absolute/path/evaluation.json
.venv/bin/python scripts/benchmark_real_documents.py --report-only \
  --db /absolute/path/evaluation.sqlite --input /absolute/path/evaluation.json \
  --output /absolute/path/rescored.json
```

Each completed row is checkpointed through atomic replacement. Same-configuration resume
preserves prior timings; corrupt existing checkpoints fail explicitly. Rescoring binds to
exact recorded job IDs, not “latest matching source”; manifest drift is refused unless
explicitly requested. Reports retain proposed/rejected claims, quote locators, pass spans,
prompt hashes, usage, failures and per-label exposure/recovery. Record the application
revision separately: a same-key rerun does not rerun terminal jobs after a code upgrade.

The recorded full local evidence (SQLite captures, exact input audit, reports, diagnostic
responses and validation logs) is at:
`/home/pop/.hermes/artifacts/harvest-real-validation/`.
The committed JSON is a compact result index, not a replacement for that evidence.

The independent public metadata/refresh/replay check also passed on current main:
84 HTTPX observations, HTTP 304 refresh with zero received body bytes, offline replay with
zero requests, and SQLite integrity `ok`. No deployment or public CI run is claimed.

Sources for the existing-tool check:
- https://docs.ollama.com/api/openai-compatibility
- https://github.com/beir-cellar/beir
- https://sbert.net/docs/sentence_transformer/usage/mteb_evaluation.html
