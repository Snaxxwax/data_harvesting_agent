# Harvest Platform

A self-hosted harvesting service that turns objectives or seed URLs into durable jobs,
structured observations, raw evidence, and inspectable research decisions.

**Status: tested 0.5 foundation, with bounded multi-pass document reading.** Durable acquisition,
offline replay, resumable record batches and audited model inputs are implemented. This is not yet a fully hardened general-purpose
research product. Current capabilities and the limits of verification are explicit below.

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No model or search account is
needed for seed-based jobs. Run from the repository root:

```bash
uv sync --frozen
uv run harvest run examples/python-release.json --key python-demo --export python-results.jsonl
```

The command prints the job ID, status, counters, and missing fields. The JSONL includes
field values, source URLs, extraction methods, confidence, evidence locators, and capture
IDs. Repeat the same command and key to return the existing job without issuing new
requests. Use a new key to refresh; unchanged assertions are deduplicated and new
retrievals remain visible as sightings. HTTP 304 reuses the previously captured body.

```bash
uv run harvest run examples/github-project.json --key httpx-first
uv run harvest status JOB_ID
uv run harvest events JOB_ID
uv run harvest resume JOB_ID
uv run harvest export JOB_ID results.jsonl
uv run harvest backup backup.sqlite
```

`resume` continues an interrupted active job. A terminal job remains terminal. A fresh
submission creates a new budget and retrieval history. Non-completed runs exit with code
2 while retaining intermediate results.

## Recover and re-extract evidence

Permitted, bounded source responses are now committed **before parsing**. A malformed or
unsupported source remains inspectable even if extraction fails. After installing an
improved trusted adapter, reprocess selected captures without a new GET or model call:

```bash
uv run harvest captures JOB_ID
uv run harvest extractions JOB_ID
uv run harvest replay 1 2 --key parser-upgrade-1
```

The authenticated API equivalent is `POST /replays` with `{"capture_ids":[1,2]}` and an
optional `Idempotency-Key` header. The response is a queued job; a worker must run it.
Replay creates extraction revisions, not new HTTP captures. Original jobs, raw bytes,
retrieval times and previous interpretations remain unchanged. Replay suppresses leads,
search, models and refresh scheduling. Installed adapters are trusted code, not sandboxed.

Before upgrading, stop old API/workers and back up the database. Startup upgrades schema
1 or 2 to schema 3 through transactional migrations. Older binaries cannot open schema 3.
See [OPERATIONS.md](OPERATIONS.md).

## Enumerate larger registers

Built-in JSON and CSV jobs process the whole permitted response in batches of at most
50 records. A worker crash resumes at the last committed record without repeating its
assertions or issuing another GET. `harvest extractions JOB_ID` reports processed records,
known total, remaining records and committed batches. CSV totals stay unknown until EOF.

Use `limits.records` (default 10,000) and `limits.claims` (default 100,000) in a JSON job
specification or API replay request to bound processing across the job. Claim limits apply
to all adapters and models and commit whole batches. A budget stop retains committed rows;
use a new replay with higher limits to recover more. Partial in-progress revisions are
inspectable in job exports and do not replace current source values until extraction finishes.

## Describe an objective

Configure a SearXNG instance to discover sources without seed URLs. Its `search.formats`
must include `json`. Configure a model for unfamiliar-text extraction and recursive
gap analysis. These are optional external services, not hidden dependencies.

```bash
export HARVEST_SEARCH_URL=http://localhost:8080
export HARVEST_PRIVATE_HOSTS=localhost
export HARVEST_MODEL_URL=http://localhost:11434/v1
export HARVEST_MODEL_NAME=YOUR_INSTALLED_MODEL
export HARVEST_MODEL_USD_PER_MILLION=0
uv run harvest investigate 'Investigate HTTPX protocol support and maintenance using primary evidence' --model --mode deep_research --dataset httpx
```

For a hosted endpoint also set `HARVEST_MODEL_KEY` and a conservative upper-bound token
price. The endpoint must support Chat Completions JSON mode and `max_tokens`. Local
models may not support this contract; verify your selected model before unattended use.
No paid model was used in the recorded verification.

For precise limits use a JSON job specification, e.g. `examples/deep-research.json`.
`use_model` is explicit because it can incur cost. Without it, HTML yields deterministic
page metadata and JSON-LD; it does not pretend to answer arbitrary factual questions.

## Reread unresolved fields

A bounded model pass shows at most five passages. When requested fields remain missing
after a pass that produced new assertions in that job's extraction, the worker schedules another pass on the
same capture, querying only the unresolved fields and excluding passages already shown.
Rereading stops at `limits.reading_passes` (default 3), after a pass with no novel
observations, or when every candidate passage has been shown. Each pass is a normal task
with its own reservation, persisted exact input and span map; no new GET is issued.
Set `reading_passes` to 1 for single-pass 0.4 behavior. Missing fields are job-wide: a
field supplied by any source ends rereading on every capture, including one that may hold
a conflicting value.

## Long-running service

Create a random token and start the API and worker in separate terminals:

```bash
export HARVEST_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run harvest serve
uv run harvest worker
```

Submit through the API:

```bash
curl -sS http://127.0.0.1:8000/jobs \
  -H "Authorization: Bearer $HARVEST_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: python-api-demo' \
  --data-binary @examples/python-release.json
```

All data endpoints require the token. OpenAPI documentation is at `/docs`; execute
authenticated requests with a bearer header through your client. The API does not run
workers in background request threads. An active worker must be running to process jobs.

Docker Compose is supplied:

```bash
cp .env.example .env
# Replace HARVEST_API_TOKEN in .env with a generated token.
docker compose up --build -d
docker compose logs -f worker
```

The API binds to host loopback. Put it behind your authenticated TLS reverse proxy or
private network for remote use. Containers share a local named volume and run as a
non-root user. Docker execution was unavailable in the development environment;
the published 0.1 CI run passed its image build. The included CI also builds subsequent revisions;
check their actual results before deployment. Compose execution remains unverified.

## What works

| Capability | Implemented behavior |
|---|---|
| Targeted jobs | Seed URLs or objective-based SearXNG discovery; scoped follow-up acquisition |
| Enumeration | Resumable JSON/CSV record batches, processing quotas and progress; JSON-LD entities and body/HTTP Link pagination |
| Continuous jobs | Persistent refresh schedules, no overlapping generations, new sightings and content-change events |
| Deep Research | Recursive leads/queries, ranked source passages, original quote offsets, exact input audit, prior observations/gaps, diversity priorities and plateau stopping |
| Evidence | Durable acquisition checkpoint, SHA-256 body, response metadata, retrieval history, locators and versioned extraction membership |
| Replay | Offline deterministic re-extraction; original results preserved; revised interpretations are not fabricated retrievals |
| Conflicts | Latest usable source analysis; differing source values remain visible; failed newer extraction attempts mark retained candidates stale |
| Recovery | Transactional commits, expiring leases, heartbeat, stale-worker fencing, idempotency keys |
| Controls | Request, byte, response, time, depth, frontier, attempt, model-call, token and estimated cost limits |
| Operations | CLI, authenticated API, cursor pagination, JSONL export, cancellation, event log, backup |
| Extensions | Trusted `harvest.adapters` entry points; retrieval and reasoning are replaceable components |

## Important limits

- Single host/local disk only. Multiple worker processes may run independent jobs;
  each job processes one task at a time. No cluster or network-filesystem support.
- `completed` means the eligible frontier ran successfully. Population coverage is
  unmeasured. Missing fields are exposed; no universal completeness claim is made.
- JSON/CSV batching removes the old 100-record response cap; 100 fields per record and
  oversized-value limits remain. HTML/JSON-LD and custom adapters retain their existing
  limits. Warnings produce a partial result and retain the permitted body. JSON is parsed
  in memory within the response-size ceiling; arbitrarily large files are not supported.
- The register probe now recovers 250/250 entities, and both 5,000-row JSON/CSV probes
  recover every fixture entity. This does not prove unknown population completeness.
  See [the 0.3 measurements](docs/validation/v03-workloads.md).
- Model input now selects passages throughout the adapter's bounded text. The long-report
  fact is recovered, but a synthetic probe still misses unmatched vocabulary and some competing
  fields (12/16 quotes exposed). Selection is not full-document review. HTML/plain text still
  stops at 100,000 normalized characters, now with a warning. See [0.4 evidence](docs/validation/v04-workloads.md).
- Internal extraction tasks count toward `limits.tasks`; allow roughly two tasks per
  acquired source before reasoning/discovery work. If the frontier is full, evidence is
  still retained and the job is partial; it can be replayed later.
- Identity uses exact URLs or source-local IDs. No fuzzy merge, merge/split UI, semantic
  cross-document entity matching, relationship graph query, or independent-source score.
- Model quotes are checked for literal support, not entailment. Confidence describes
  extraction certainty, not source truth. Model observations are attached to source
  documents; they are candidate assertions. Conclusions require evidence review.
- Deep Research is an initial bounded heuristic. It does not yet have a validated
  information-gain metric, unlimited context, cross-source contradiction adjudication,
  or a benchmark demonstrating research quality.
- No browser, PDF/OCR, source authentication, CAPTCHA bypass, or generated-code execution.
  Gzip/deflate have bounded decoding; other content encodings are rejected.
- Refresh revisits initial discovery/seeds; it does not yet implement field-specific
  refresh policies or reliable deletion/tombstone semantics.
- No multi-tenant isolation, encryption at rest, metrics exporter, disk quota manager,
  or retention/garbage collection policy. Deploy only for trusted operators.

See [ARCHITECTURE.md](ARCHITECTURE.md), [SOURCE_AUDIT.md](SOURCE_AUDIT.md),
[DATA_MODEL.md](DATA_MODEL.md), [OPERATIONS.md](OPERATIONS.md),
[TEST_PLAN.md](TEST_PLAN.md), [ROADMAP.md](ROADMAP.md), and
[docs/VALIDATION.md](docs/VALIDATION.md) for evidence, decisions, and next work.

## Development

```bash
uv sync --frozen
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest -q
uv build
```

Dependencies are pinned in `uv.lock`. Tests use real local HTTP servers and a deterministic
test model endpoint; they do not spend money or require public network access.
