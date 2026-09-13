# Validation record

## 0.3 — 2026-09-13

The [0.3 workloads](validation/v03-workloads.md) now recover all 250 entities in the original
register and all 5,000 entities in each larger JSON/CSV case. Each larger case produces
25,000 observations with two HTTP requests and reproduces them by offline replay with no
new requests. [Machine-readable measurements](validation/v03-results.json) include timings,
process memory and the real schema-2 database upgrade. Current automated results are in
[test-summary.json](validation/test-summary.json).

The schema-2 acceptance database upgraded on a new copy with 89 observations, 173 sightings,
three captures, four extraction revisions and all four online/replay keys preserved. Record
progress was not invented for old runs. Crash/fault, processing budgets, late malformed CSV,
partial refresh, cursor provenance and migration rollback tests passed. No fresh public
request was attempted for this milestone; owned-source HTTP and historical-evidence upgrade
are the validation scope. Deep Research's long-document omission remains reproducible.

The [published 0.2 CI run](https://github.com/Snaxxwax/data_harvesting_agent/actions/runs/34753480469)
passed tests, package and Docker builds. Later revisions have their own CI outcomes.

## 0.2 — 2026-09-13

See [four-mode before/after results](validation/v02-workloads.md),
[upgrade report](validation/v02-upgrade.json), and the current
[test summary](validation/test-summary.json). The 0.1 public acquisition evidence below
is retained as history, not presented as fresh 0.2 network results.

The actual 0.1 acceptance database was backed up and migrated: 89 observations, 173
sightings, all three captures and idempotency keys were preserved; integrity and foreign-key
checks passed. Original schema-1 data was not modified. A fresh public-source check in this
session timed out after three attempts, receiving zero bytes. It is not counted as a pass.
Owned-source HTTP workloads and offline replay verification do not require public egress.

The published 0.1 [GitHub CI run](https://github.com/Snaxxwax/data_harvesting_agent/actions/runs/34735441601)
passed tests, distribution builds and Docker build (checked 2026-09-13). This does not
establish Compose runtime behavior, deployment, or the conclusion of later commits.

## Public acquisition

These were actual network jobs executed by the platform, not canned fixture responses.
They used the workspace's trusted remote-DNS proxy restricted to `www.python.org` and
`api.github.com`, with TLS certificate verification enabled. Robots requests were included
in the budget. Raw response representations are persisted in the local acceptance database.
Committed capture metadata and hashes are in [validation/public-runs.json](validation/public-runs.json).

| Job | Result | Observations | Requests | Received body bytes |
|---|---|---:|---:|---:|
| Python.org homepage metadata and JSON-LD | completed | 5 | 2 | 12,099 |
| HTTPX official GitHub repository metadata | completed | 84 | 2 | 6,170 |
| Repeat GitHub acquisition under a new job key | completed, HTTP 304 | 84 | 1 | 0 |

The database contains **89 unique observations and 173 sightings** after these jobs.
The refresh points to the previous capture and reuses its body hash. Repeating each
original idempotency key generated zero additional requests. SQLite `integrity_check`
returned `ok`. Counts describe the responses actually received at verification time,
not permanent properties of those sources.

## Automated verification

The final test count and runtime are recorded in `validation/test-summary.json`.
The suite covers real local HTTP, state recovery, process kill during a transaction,
conflicts, conditional refresh, source limits, model/search integration contracts,
resource reservations, robots, redirects, SSRF, API auth, and backup restoration.
Ruff lint and formatting pass. Wheel and source distribution build successfully.

Two deprecation warnings arise from Starlette's HTTPX-based TestClient and its AnyIO
portal alias. They did not fail validation; update the test client integration when the
supported migration path is adopted. They are not suppressed in the suite.

## What this does not establish

- The Compose stack was not executed because Docker is unavailable here. The 0.1 Docker
  image build subsequently passed in GitHub CI; see the dated update above.
- No live LLM or live SearXNG instance was configured. Model/search contracts were exercised
  against actual local HTTP endpoints with controlled responses; research quality remains
  unbenchmarked. No model charges were incurred.
- No prolonged production soak, throughput study, full disk failure experiment, multi-host
  deployment, or comprehensive security/dependency audit has been performed.
- The initial GitHub publication blocker was resolved. The canonical repository is
  [Snaxxwax/data_harvesting_agent](https://github.com/Snaxxwax/data_harvesting_agent).
  Publishing the repository does not deploy a service.

## Corrections driven by verification

The initial public attempt failed because local DNS was unavailable behind the proxy.
An exact-host proxy path and correct system CA handling resolved it without disabling TLS.
A server returned gzip despite an identity request; bounded standard-library decoding now
handles gzip/deflate and rejects oversized output. The original 50-field cap produced a
partial GitHub record; the adapter now supports 100 fields per record with a 5,000-assertion
response limit and explicit warnings. JSON Pointer root handling, fragment identity,
pagination depth and elapsed-deadline handling were corrected and regression tested.
