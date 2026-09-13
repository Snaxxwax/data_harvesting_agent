# Operations

## Supported topology

One host, local filesystem, trusted operator. SQLite is not supported on NFS/SMB or a
shared multi-host volume. The API and workers must use the same database. Docker Compose
provides a loopback-bound API, worker, local named volume, restart policy and log rotation.
Docker runtime validation remains a deployment gate; only Python execution was available
during development. The base image and GitHub Actions major tags are not digest-pinned.

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

## Status and inspection

`GET /jobs/{id}` returns counts, reserved resources, captures, missing requested fields and
status. `/events`, `/tasks`, `/observations`, and `/datasets/{name}/entities` provide bounded
pages. Events use an integer `after` cursor; observations/entities use the last record ID.
`/captures/{id}` provides metadata and `/captures/{id}/body` downloads inert evidence bytes.
`/jobs/{id}/export` streams JSONL. Export a terminal job for a stable complete snapshot;
new observations created behind a cursor can otherwise be omitted from an in-flight export.

`POST /jobs/{id}/cancel` cancels that generation. For continuous jobs, separately call
`POST /schedules/{id}/disable` (the first job ID is the schedule ID) to stop future runs.
The worker executes due schedules after restart, without overlapping active generations.

Failure categories are durable. Policy blocks do not retry. Transient HTTP/network errors
retry with backoff and jitter, honoring Retry-After for affected origins. Invalid adapter
output fails the task while other work may continue. Source response bodies are committed
only after valid extraction; malformed/unsupported extraction failures currently retain
failure metadata, not a raw capture. A budget stop retains all previously committed evidence.

## Backup and upgrades

Use `harvest backup /backup/harvest.sqlite`, which uses SQLite's online backup API to include
evidence and history consistently. For restoration, stop API/workers, replace the database
with the backup, ensure UID 10001 can write the file and directory, then restart. A backup
is tested by opening it and comparing stored observations and raw bodies to the original.
Keep backups off the service disk and monitor free space. No retention policy or automatic
pruning is installed; continuous jobs can grow the database indefinitely.

Run `uv sync --frozen` for upgrades from an reviewed commit. Validate database migrations
against a restored copy first. This release only recognizes schema version 1. Package
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
