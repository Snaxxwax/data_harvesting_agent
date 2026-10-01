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
`compose.yaml` works from a fresh checkout with no `.env` present: it falls back to a
clearly-labeled, publicly-visible default `HARVEST_API_TOKEN`/`HARVEST_USER_AGENT` via
an optional `env_file` and `${VAR:-default}` substitution. Override both in `.env` for
anything beyond a single trusted local operator. The browser-UI milestone built the image,
started the stack with no `.env`, and smoke-tested the API and browser UI end to end
(`docker compose config`, `up`, login, job create/cancel/rerun, JSONL/CSV export); the
published 0.1 Docker image build separately passed in GitHub CI. Re-verify in your own
deployment environment and check current CI results before relying on this. The base image
and GitHub Actions major tags are not digest-pinned.

## Configuration

| Environment variable | Meaning |
|---|---|
| `HARVEST_DB` | SQLite path, default `data/harvest.sqlite` |
| `HARVEST_API_TOKEN` | Required for API; at least 24 characters, use a random secret |
| `HARVEST_USER_AGENT` | Crawler identity; set a contact-bearing identifier for your deployment |
| `HARVEST_SEARCH_URL` | Optional SearXNG base URL, JSON format enabled |
| `HARVEST_PRIVATE_HOSTS` | Exact administrator-approved hosts allowed to resolve privately; default empty |
| `HARVEST_TOOLS` | External OSINT CLIs permitted as acquisition tasks (e.g. `maigret`); default empty, meaning none |
| `HARVEST_TOOL_PACKAGES` | Build-time only: pinned tool packages to install into the image; default empty |
| `HARVEST_TOOL_TIMEOUT` | Wall-clock seconds one tool run may take before the task fails; default 300 |
| `HARVEST_MAIGRET_RETRIES` | Retry transient failures for individual Maigret sites, 0–3; default 0 |
| `HARVEST_MAIGRET_CLOUDFLARE_BYPASS` | Pass Maigret `--cloudflare-bypass` when true; default false; requires a separately configured local bypass service |
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

The browser UI at `/` authenticates with a session cookie instead of a bearer header.
`POST /session` checks the submitted value against `HARVEST_API_TOKEN` with a
constant-time comparison, then issues an HMAC-signed, 12-hour, HttpOnly, `SameSite=Strict`
cookie keyed on that token (`src/harvest/sessions.py`); the raw token itself is never put
in a cookie, rendered HTML, JavaScript, or a log line. `POST /logout` clears it. Every
existing bearer-authenticated request keeps working unchanged; the cookie is only a second,
narrower way to satisfy the same `authenticate` dependency, and sessions do not survive an
API token rotation (the signature no longer verifies).

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

`GET /jobs/{id}/records` projects that job's own observations into per-entity fields with
`value`/`conflict`/`missing`/`candidates` (mirroring `/datasets/{name}/entities` but scoped
to one job, like `/jobs/{id}/dossier`). Every field in the job's spec is backfilled onto
every entity, `missing: true`, even one with zero observations anywhere in the job, so a
caller never has to infer absence from a dict key that just isn't there. `GET
/jobs/{id}/export.csv` renders the same projection as a record-oriented CSV: two identity
columns, then `<field>`, `<field>__status` (`ok`/`conflict`/`missing`) and `<field>__sources`
per field observed or requested anywhere in the job, with basic
spreadsheet-formula-injection neutralization on both data cells and header cells (field
names come from `JobSpec.fields`, which is caller-controlled, so the derived `<field>`,
`<field>__status` and `<field>__sources` header names are neutralized exactly like a value).
`POST /jobs/{id}/rerun` resubmits that job's exact stored spec as a new durable job (a new
idempotency-free submission); it rejects `mode=continuous` jobs, which already refresh
through their schedule, to avoid creating a second overlapping schedule. `GET /meta` reports
whether search/model are configured, for UI/client-side feature gating. `POST
/plan/investigation` and `POST /plan/dataset` expose the deterministic intake/planning
module (`harvest.planning`) that the browser UI's launcher uses to turn free text into
seeds, bounded discovery queries and default fields, without ever calling a model.

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
`POST /replays` with `capture_ids` and optional `limits` or `investigation`. The CLI accepts
`--investigation PATH` containing a JSON `Investigation`. Replay accepts up to 100 fetch or
tool captures from one dataset, not search responses. CLI `--submit-only` queues for workers.
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

## External tools

`HARVEST_TOOLS` names the CLIs a deployment permits, but the binary must also exist in the
image. It is not a project dependency: harvest executes these by argv as a subprocess, so
they are installed as isolated `uv` tools under `/opt/uv-tools` and never resolved against
the project's own pins. maigret alone adds 28 transitive packages and roughly 240 MB, and
depends on `socid-extractor<0.2.0`, which would otherwise couple the profile adapter's
pinned version to maigret's range.

Build the image with the tools you intend to enable, then enable them at runtime:

```bash
HARVEST_TOOL_PACKAGES="maigret==0.6.6" docker compose build
HARVEST_TOOLS=maigret docker compose up -d
```

Both variables are required. Building without `HARVEST_TOOL_PACKAGES` keeps the default
image lean; enabling `HARVEST_TOOLS` for a tool absent from the image fails that task with
a clear "not installed in this worker image" error rather than silently skipping it.

Tool runs are exempt from request, byte and per-origin pacing budgets, because the binary
makes its own requests outside the fetcher. `limits.tool_runs` bounds actual invocations and
`HARVEST_TOOL_TIMEOUT` bounds each one; nothing else throttles them. Treat the site coverage
of a tool like maigret as authorization-relevant, not just a volume question.

When `HARVEST_EGRESS_PROXY` is set, harvest passes it to Maigret as `--proxy` for the main
site checks. It also disables Maigret's automatic site-database update: in pinned version
0.6.6 that update ignores `--proxy` and otherwise contacts GitHub directly. Proxy mode
therefore uses Maigret's bundled site database until a newer image is built. Maigret's
auxiliary activation requests do not receive `--proxy` and may still use the direct route.
Ambient proxy variables are cleared because they can interfere with Maigret's explicit
proxy connector. `HARVEST_PROXY_PUBLIC_HOSTS` governs the fetcher, not Maigret's site list.
For a strict no-direct-egress guarantee, run the worker on authorized infrastructure with
network rules that permit outbound traffic only to the proxy.

Maigret receives `--retries` from `HARVEST_MAIGRET_RETRIES` (default 0). These are
retries of temporarily failed site checks inside one scan; they can increase requests
but never restart the whole durable tool task. Values above 3 are rejected before the
binary starts. `HARVEST_MAIGRET_CLOUDFLARE_BYPASS=true` passes
`--cloudflare-bypass` only when explicitly enabled. Maigret 0.6.6 requires a separate
local Cloudflare bypass service configured in Maigret's own `settings.json`; the bundled
settings point at localhost ports 8191 and 8000, which are inside the worker container
under Compose. The flag alone does not start those services or route their traffic
through `HARVEST_EGRESS_PROXY`. Verify the bypass service's egress separately.

A tool task is never retried, including after a worker lease expires. A timeout and a
nonzero exit both fail the task permanently,
because one attempt already costs hundreds to thousands of unbudgeted third-party requests
and a scan that overran its wall clock is no likelier to fit on a second try. A nonzero exit
is rejected even when a report file was left behind, so an aborted scan is never recorded as
a completed capture. Resubmit the job to run the tool again.
Cancelling a job revokes the tool task's lease; the worker checks ownership once per second
and terminates the tool process group when it loses ownership.

maigret runs with `--all-sites`: every known site rather than the top-ranked default. That
is roughly ten times the sites and so roughly ten times the outbound requests, none of which
pass through the fetcher's budgets or per-origin pacing. It also roughly doubles what a scan
finds. Treat one run as conspicuous traffic from the deployment's address.

A scan fits the compose worker's `mem_limit: 512m`. Measured inside a read-only,
`cap-drop ALL`, non-root container with swap disabled, counting the worker and the tool
together: the full 5203-site set peaks at 309 MiB, against 227 MiB for the top-ranked
default. Usage is flat across a scan rather than accumulating, so it is bounded by the
tool's own concurrency and not by result volume. Measure again before lowering the limit or
adding a tool that downloads media.

A full run took 116 seconds against the default 300 second `HARVEST_TOOL_TIMEOUT`. That
margin depends on the link: a slow or rate-limited network can overrun the timeout, and an
overrun now fails the task permanently rather than retrying, so raise
`HARVEST_TOOL_TIMEOUT` rather than letting scans fail.

The container runs read-only as a non-root user with no home directory, so `HOME` is set to
the `/tmp` tmpfs: maigret creates its site-database directory on startup and aborts with a
read-only-filesystem error otherwise. That cache is expendable and is re-fetched per
container, costing roughly 2.5 MB inside the default 64 MB tmpfs.

## Source adapters

The installed `socid-html/1` adapter adds profile fields from captured HTML to the built-in
title, JSON-LD, text and links. It uses `socid-extractor` on the stored body; it does not
fetch pages, follow the library's URL mutations, or use its optional AI fallback. Fields
whose values cannot be found literally in the captured HTML are omitted with a warning.
The capture and extraction revision remain available for review.
The initial offline fixture results are in `docs/validation/v06-profile-identity.md`.

Profile identifiers do not automatically join accounts or investigation targets. To use
one in an exact-match dossier, inspect an HTTP profile capture's original `url` and
observations, then declare the exact identifier and permitted source field mapping. For
example, save this as `mal-investigation.json` for a captured profile whose original URL
was `https://myanimelist.net/profile/Xinil`:

```json
{
  "targets": [{"key": "mal_account", "label": "Declared MAL account",
               "identifiers": {"myanimelist.uid": ["1"]}}],
  "sources": [{"url": "https://myanimelist.net/profile/Xinil",
               "identifier_fields": {"mal_uid": "myanimelist.uid"},
               "field_map": {"mal_uid": "platform_id", "mal_username": "username"}}]
}
```

Run `harvest replay CAPTURE_ID --investigation mal-investigation.json` and then
`harvest dossier REPLAY_JOB_ID`. The API equivalent is `POST /replays` with
`{"capture_ids":[CAPTURE_ID],"investigation":{...}}`, followed by
`GET /jobs/{id}/dossier`. Replay reads stored bytes and creates a new job-scoped
interpretation; the original job stays intact. Use the original capture `url` in the
source rule, even if its `final_url` differs after redirects. A username shared across
platforms is never treated as evidence that the accounts belong to one person.

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
