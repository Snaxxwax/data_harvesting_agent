# Harvest Platform

A self-hosted harvesting service that turns objectives or seed URLs into durable jobs,
structured observations, raw evidence, and inspectable research decisions.

**Status: tested 0.1 foundation, with a single-host deployment boundary.** The initial
end-to-end milestone is implemented. This is not yet a fully hardened general-purpose
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
the included CI builds the image when pushed to GitHub.

## What works

| Capability | Implemented behavior |
|---|---|
| Targeted jobs | Seed URLs or objective-based SearXNG discovery; scoped follow-up acquisition |
| Enumeration | JSON/CSV records, JSON-LD entities, body/HTTP Link pagination; bounded frontier |
| Continuous jobs | Persistent refresh schedules, no overlapping generations, new sightings and content-change events |
| Deep Research | Recursive model-proposed leads and queries, diversity/relevance priorities, prior observations and gaps as context, quote checking, persisted reasoning and plateau stopping |
| Evidence | SHA-256 raw body, response metadata, retrieval time, previous capture, field locator and extractor version |
| Conflicts | Latest-source candidate view; differing values remain visible and canonical value is unset |
| Recovery | Transactional commits, expiring leases, heartbeat, stale-worker fencing, idempotency keys |
| Controls | Request, byte, response, time, depth, frontier, attempt, model-call, token and estimated cost limits |
| Operations | CLI, authenticated API, cursor pagination, JSONL export, cancellation, event log, backup |
| Extensions | Trusted `harvest.adapters` entry points; retrieval and reasoning are replaceable components |

## Important limits

- Single host/local disk only. Multiple worker processes may run independent jobs;
  each job processes one task at a time. No cluster or network-filesystem support.
- `completed` means the eligible frontier ran successfully. Population coverage is
  unmeasured. Missing fields are exposed; no universal completeness claim is made.
- Built-in structured extraction caps each response at 100 records and 100 fields per
  record. Limit warnings cause a `partial` result and retain the full permitted body.
  Large populations need paginated APIs or a dedicated streaming adapter.
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
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest -q
uv build
```

Dependencies are pinned in `uv.lock`. Tests use real local HTTP servers and a deterministic
test model endpoint; they do not spend money or require public network access.

