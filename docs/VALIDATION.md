# Validation record — 2026-09-12

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

- The Docker image/Compose stack was not executed because Docker is unavailable here.
  CI is prepared to build the image once the repository is on GitHub.
- No live LLM or live SearXNG instance was configured. Model/search contracts were exercised
  against actual local HTTP endpoints with controlled responses; research quality remains
  unbenchmarked. No model charges were incurred.
- No prolonged production soak, throughput study, full disk failure experiment, multi-host
  deployment, or comprehensive security/dependency audit has been performed.
- GitHub repository creation/push is blocked by missing creation capability in the connected
  integration. A local Git repository and transfer bundle are prepared. No remote repository
  or deployed service is claimed.

## Corrections driven by verification

The initial public attempt failed because local DNS was unavailable behind the proxy.
An exact-host proxy path and correct system CA handling resolved it without disabling TLS.
A server returned gzip despite an identity request; bounded standard-library decoding now
handles gzip/deflate and rejects oversized output. The original 50-field cap produced a
partial GitHub record; the adapter now supports 100 fields per record with a 5,000-assertion
response limit and explicit warnings. JSON Pointer root handling, fragment identity,
pagination depth and elapsed-deadline handling were corrected and regression tested.
