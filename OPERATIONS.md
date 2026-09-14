# Operations

## 0.4 evidence selection and upgrade

Stop workers before updating code, keep a normal database backup, then run `uv sync --frozen`
and restart. Database schema stays 3; no new migration, dependency, model account or service
is required. Existing completed jobs and offline replays remain intact. Pending reasoning
uses the installed selector. New model observations identify `model/2:<model-name>`.

Reasoning uses at most 14,000 characters of selected source text per call, plus schema and
research context. Global token/cost/call budgets still govern dispatch; this source bound
is fixed rather than a new job setting. The selector uses SQLite FTS5 when available and
records `coverage-fallback-no-fts5` in audit metadata otherwise. Full-text search installation
is unnecessary for deterministic acquisition/extraction jobs.

Inspect `model_reserved` in the existing job events API/CLI for exact model input, source
spans, omission counts, normalizer, hashes, attempt and reservation. These events now contain
source passages and stored observation context, so protect exports/backups like raw evidence.
Authorization headers and model API keys are not recorded. Audit storage grows with each
attempt; retention/disk quotas remain unimplemented. A reservation is not proof of provider
execution, and uncertain attempts are not refunded or automatically response-cached.

HTML/plain-text sources over the existing 100,000-character normalization bound now produce
an omission warning and a partial extraction. This makes a prior silent omission visible;
the raw acquired representation remains available. Selection omissions within that bound
appear in the prompt and audit, and do not by themselves mark a successfully executed job partial.

## Supported topology

One host, local filesystem, trusted operator. SQLite is not supported on NFS/SMB or a
shared multi-host volume. The API and workers must use the same database. Docker Compose
provides a loopback-bound API, worker, local named volume, restart policy and log rotation.
Docker runtime validation remains a deployment gate; local Docker execution is unavailable.
The published 0.1 Docker image build passed in GitHub CI, but Compose was not smoke-tested.
The base image and GitHub Actions major tags are not digest-pinned.

## Configuration

| Environment variable | Meaning |
|---|---|
| `HARVEST_DB` | SQLite path, default `data/harvest.sqlite` |
| `HARVEST_API_TOKEN` | Required for API; at least 24 characters, use a random secret |
| `HARVEST_USER_AGENT` | Crawler identity; set a contact-bearing identifier for your deployment |
| `HARVEST_SEARCH_URL` | Optional SearXNG base URL, JSON format enabled |
| `HARVEST_PRIVATE_HOSTS` | Exact administrator-approved hosts allowed to resolve privately; default empty |
| `HARVEST_MODEL_URL` | Trusted Chat Completions base URL ending in `/v1` where appropriate |
| `HARVEST_MODEL_NAME` | Model identifier accepted by that endpoint |
| `HARVEST_MODEL_KEY` | Optional bearer secret; never stored in a job or capture |
| `HARVEST_MODEL_USD_PER_MILLION` | Upper-bound token price across input/output/reasoning; 0 for local inference |
| `HARVEST_CA_BUNDLE` | Optional CA file; otherwise the system TLS trust store is used |
| `HARVEST_EGRESS_PROXY` | Optional administrator-controlled egress proxy; ambient proxy env is ignored |
| `HARVEST_PROXY_PUBLIC_HOSTS` | Exact public hostnames permitted through that proxy; default empty |

Service/model endpoints are administrator configuration, never model output. A configured
SearXNG host on the private network must also be in `HARVEST_PRIVATE_HOSTS`. In containers,
`localhost` refers to that container; use your actual service DNS or explicitly configured
host gateway. No model is silently downloaded or paid API account provisioned.

The remote-DNS proxy path permits only listed public hostnames. The proxy operator must
enforce public destination addresses; this path cannot pin the proxy's resolved IP from
inside the application. Direct acquisition validates every DNS result and connects to an
approved numeric IP. Redirects are rechecked. Use direct mode for broad autonomous public
discovery or configure a policy-enforcing proxy with an explicit host inventory.

## Job budgets

Requests include robots fetches, redirect hops, search and model calls. Retries count.
Response bytes count received wire-body chunks; gzip/deflate decode into a separately
bounded representation. The reader can receive a final 64 KiB chunk before detecting a
byte limit, plus transport buffering. Counters are an application limit, not a packet-level
network quota. `response_bytes` bounds both encoded and decoded response bodies.

Time starts at the first task claim and includes downtime. Checks happen between network
operations/chunks and during idle worker ticks. An already-blocked socket can last through
its timeout; this is cooperative cancellation, not a hard real-time deadline. A source
request's configured timeout is 20s; model network timeout is 60s. Synchronous DNS or a
trusted extension can block outside these checks. Use container/resource/process controls
for stricter isolation. Workers finish the current task after SIGTERM when possible; SIGKILL
leaves it for lease recovery.

Model reservations use UTF-8 input bytes plus overhead as a conservative token estimate,
the output-token cap and the configured worst-case price. Reservations survive failure and
restart and are not refunded; provider-reported usage is kept separately in audit events.
This bounds application-authorized spending only if the provider honors the output cap
and the configured price covers its billing. Set provider-side limits too for paid endpoints.
SearXNG query charges, infrastructure, electrical power and other externally billed services
are not estimated by this ledger.

`limits.records` (default 10,000) bounds native JSON/CSV source rows visited per job;
`limits.claims` (default 100,000) bounds emitted assertions across all extraction methods,
including duplicate assertions and model output. Claim budget checks and processing counters
commit atomically with results. A whole batch must fit the claim budget, so some remaining
quota may go unused. Up to 50 records form one batch, within the existing extraction task.
Row batching does not consume additional task slots or attempts. Body/request/time limits
remain independent. JSON is buffered and parsed once per attempt, not streamed from disk.

## Status and inspection

`GET /jobs/{id}` returns counts, reserved resources, captures, missing requested fields and
status. `/events`, `/tasks`, `/captures`, `/extractions`, `/observations`, and `/datasets/{name}/entities` provide bounded
pages. Events use an integer `after` cursor; observations/entities use the last record ID.
`/captures/{id}` provides metadata and `/captures/{id}/body` downloads inert evidence bytes.
`/jobs/{id}/export` streams JSONL. Export a terminal job for a stable complete snapshot;
new observations created behind a cursor can otherwise be omitted from an in-flight export.

`/jobs/{id}/extractions` additionally returns `records_processed`, `records_total`,
`records_remaining` and `batches`. NULL means unknown, including legacy/custom-adapter
record counts and CSV totals before EOF. Processing an entire response is not evidence
that all external population members or requested facts have been discovered.
On partial failure or cancellation, exports may contain committed prefixes of unfinished
revisions. Inspect extraction status before using those rows downstream. Canonical views
do not publish an unfinished batch revision over the last usable source interpretation.

`POST /jobs/{id}/cancel` cancels that generation. For continuous jobs, separately call
`POST /schedules/{id}/disable` (the first job ID is the schedule ID) to stop future runs.
The worker executes due schedules after restart, without overlapping active generations.

Failure categories are durable. Policy blocks do not retry. Transient HTTP/network errors
retry with backoff and jitter, honoring Retry-After for affected origins. Invalid adapter
output fails extraction while other work may continue. Successful permitted source responses
(HTTP 200 or reused 304) are checkpointed before parsing. Malformed/unsupported bodies survive
with a failed extraction record. Rejected oversized bodies, failed HTTP requests and invalid
search-service responses are not guaranteed captures. A budget stop retains prior checkpoints.

Use `harvest replay CAPTURE_ID... --key KEY` after installing an improved adapter, or submit
`POST /replays` with `capture_ids` and optional `limits`. Replay accepts up to 100 source
captures from one dataset, not search responses. CLI `--submit-only` queues for workers.
Replay has zero acquisition/model cost counters; task/time/attempt and processing limits still apply.
Stored-body reads are not billed as network bytes. Internal extraction tasks also count
toward the ordinary task frontier. If that cap prevents extraction, find the retained body
through `/jobs/{id}/captures` and replay it with a sufficient task budget.

Replay always uses currently installed deterministic adapters. It does not repair malformed
JSON automatically and does not regenerate model claims. Prior model assertions remain in
the old revision. Review revised results before downstream use; a successful new interpretation
is eligible for the current view even when it omits fields. There is no revision-approval UI.

## Backup and upgrades

Use `harvest backup /backup/harvest.sqlite`, which uses SQLite's online backup API to include
evidence and history consistently. For restoration, stop API/workers, replace the database
with the backup, ensure UID 10001 can write the file and directory, then restart. A backup
is tested by opening it and comparing stored observations and raw bodies to the original.
Keep backups off the service disk and monitor free space. No retention policy or automatic
pruning is installed; continuous jobs can grow the database indefinitely.

Run `uv sync --frozen` for upgrades from an reviewed commit. Validate database migrations
against a restored copy first. Stop all old API/worker processes before upgrading; mixed-version
operation is unsupported. This release migrates schema 1 -> 2 -> 3 automatically, each step transactionally;
legacy raw evidence, observation IDs, sightings, keys and schedules are preserved. Use
`uv run python scripts/verify_upgrade.py OLD_DB NEW_COPY` to exercise migration on a new copy
without modifying OLD_DB (schema 1 or 2). Legacy record progress is unknown, not reconstructed
as zero or complete; legacy claim counters use stored assertion membership counts. The new
default processing limits apply to active legacy jobs too. Roll back by restoring the pre-upgrade backup and old code together;
there is no down-migration. Package
updates require rerunning the recovery and provider contract tests; a lockfile does not
replace supply-chain review.

## Source adapters

Install trusted packages exposing an entry point:

```toml
[project.entry-points."harvest.adapters"]
custom = "my_package:MyAdapter"
```

An adapter implements `accepts(content_type, url)` and `extract(body, url) -> Extraction`,
with an explicit extractor name/version. It operates on already acquired bytes and must
not perform its own IO. Validate with source fixtures, stable identities, exact evidence
locators, limit handling and changed-content cases. The boundary is a trusted Python plugin
contract, not a sandbox. Do not install source-supplied or model-generated code.
