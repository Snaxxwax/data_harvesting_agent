# Architecture hypothesis and implementation

Initial decision date: 2026-09-12. Revised 2026-09-13. Implemented version: 0.3.0.

## Runtime boundary

The first deployment is a trusted-operator service on one Linux host with local durable
storage. Python was selected for its HTTP, parsing, data-validation and model ecosystem,
and because the smallest integration could be exercised with real process and network
failures. This choice does not make a Python agent framework the system of record.

FastAPI serves requests. Independent worker processes execute persistent tasks. The CLI
uses the same Engine and Store. Each job permits one active task at a time; different jobs
can run across workers. This bounds per-job budget and research-state races without a
distributed scheduler. SQLite's short `BEGIN IMMEDIATE` transactions serialize writes.

```mermaid
flowchart TD
  Operator["Objective and limits"] --> Jobs["Durable job and frontier"]
  Jobs --> Worker["Leased worker"]
  Worker --> Acquire["HTTP or search"]
  Acquire --> Checkpoint["Capture and extraction task commit"]
  Checkpoint --> Extract["Deterministic extraction"]
  Extract --> Commit["Atomic assertion revision commit"]
  Replay["Offline replay"] --> Extract
  Commit --> Model["Optional bounded reasoning"]
  Model --> Jobs
  Commit --> Jobs
  Commit --> Results["Observations and candidate view"]
```

## Reuse and custom code

- HTTPX/HTTPCore own HTTP, TLS, connection pooling and streaming. A small public transport
  adapter pins validated IPs through HTTPCore's network backend extension point.
- Beautiful Soup owns HTML parsing; Python's CSV/JSON libraries own structured parsing.
- Protego owns robots rule parsing, including wildcard precedence and crawl delays.
- Pydantic validates specifications, extraction output and model proposals.
- SQLite owns transactions, persistence and recovery; FastAPI/Uvicorn own the HTTP API.
- SearXNG is an optional external search service. A small adapter consumes its API.
- Model reasoning uses a constrained Chat Completions contract; provider routing can be
  supplied by a local server or an external compatible gateway.
- Custom work is the harvesting state machine, observation history, scope policy, source
  frontier and budget accounting. It is not a generic distributed queue implementation.

The source audit records why a crawler's request queue was not made authoritative for
evidence commits. Crawlee remains the strongest candidate for a future browser/large-crawl
adapter. PostgreSQL becomes warranted when measured concurrency, retention or multi-host
requirements exceed this implementation. Do not claim the current Store is portable SQL.

## Commit and failure semantics

1. Claim a task in a transaction and issue a random lease token.
2. Reserve each network request before dispatch; persist retry counts and budget usage.
3. Acquire outside the transaction, subject to scope, robots, throttling and size checks.
4. Commit raw body, capture, extraction task/revision, acquisition completion and events
   in one transaction. If the frontier cap prevents extraction, retain evidence and report partial.
5. A separately leased task parses the stored body. Commit observations, extraction
   membership, sightings, follow-up tasks and processing progress atomically. Built-in JSON/CSV
   uses at most 50 records per transaction. The same lease and parser iterator continue across
   batches; only the final batch completes the task and publishes its interpretation.
   Invalid/expired tokens reject each boundary. Parsing failures leave captures and prior
   committed batches intact. Non-batch adapters retain a single extraction commit.
6. If a worker dies, its lease expires. Another worker reclaims the task up to the attempt
   limit. A heartbeat normally extends a lease every 15 seconds; default lease is 120s.

Acquisition is **at least once** across crashes. An external server may receive a GET
that the local worker never records as a capture. Database results are idempotent under
the task/observation keys. There is no claim of exactly-once external execution. Model
calls have the same uncertain-outcome problem; reserved cost is not refunded on failure.

This revises the original single-transaction hypothesis based on observed evidence loss;
see [ADR 0002](docs/adr/0002-durable-acquisition-and-replay.md). A crash after the acquisition
checkpoint requires re-extraction, not reacquisition. Offline replay jobs reference existing
captures and create new assertion memberships, never fake retrieval timestamps. Replay
uses deterministic adapters and does not enqueue leads or model work. It is not a plugin sandbox.

[ADR 0003](docs/adr/0003-resumable-record-extraction.md) records the next revision:
an extraction cursor and job-wide record/claim counters now advance with each batch.
On lease recovery the parser restarts once and skips the committed ordinal. Normal execution
parses JSON once per lease attempt, rather than once per batch. Body hashes are checked
before applying a cursor. Completed source views exclude unfinished revisions, even if those
revisions contain inspectable committed assertions. Earlier source values remain marked stale.

`completed`, `partial`, `failed`, `cancelled`, `budget_exhausted`, and `plateau` are terminal.
Job creation is idempotent only when the caller provides the same key and spec. Changing
the spec under the same key is rejected. Reprocessing a terminal job requires a new job.

## Evidence and reconciliation

Captured response representations (after bounded content decoding) are content-addressed BLOBs in SQLite for the milestone. This allows atomic
evidence/checkpoint commits and coherent backups. Bounded responses keep individual writes
manageable. This deliberately trades large-scale object storage efficiency for a smaller,
testable failure surface. Object storage needs a staged-write protocol before adoption.

Observations retain field, typed value, evidence, locator, method, extractor version,
confidence and source URL. Sightings connect an observation to every capture where it was
seen. `extractions` and `assertions` distinguish interpretations of the same capture.
Canonical read views use the latest usable extraction of each source's latest successfully
interpreted retrieval; pending/failed newer attempts are surfaced as stale source states.
A newer replay cannot make an older retrieval fresher than a later retrieval. Disagreement produces
multiple candidates and an unset canonical value. This is neither voting nor truth scoring.

## Research controller

Objective-only jobs first search through configured SearXNG. Retrieval and extraction remain
deterministic. Reasoning tasks receive bounded source text, a sample of existing observations,
requested-field gaps, recent decisions and visited/queued sources. The model proposes
quoted claims, leads, queries, gaps and contradiction notes. Proposals cannot alter budgets,
host policy, executable code, authentication or operator settings.

Literal quote checks reject ungrounded model quotations. They cannot prove entailment.
The result is a candidate observation. Primary-source preference is requested in the
reasoning prompt and expressed in leads; it is not a calibrated authority ranking.
Domain diversity is a hostname heuristic, not proof of source independence. Research
stops on exhausted frontier, limits, cancellation, or consecutive pages with no novel
observations. New wording/metadata can count as novelty; calibrated information gain is
future work. No quality guarantee follows from the existence of this loop.

## Scheduling and refresh

Continuous jobs create a durable schedule. The next generation starts only after the
previous job is terminal; missed intervals coalesce into one next run. Refresh starts from
the original seeds/discovery objective, retaining old evidence and using conditional HTTP
requests when validators are present. Schedule cancellation is separate from cancelling
one generation. Deletion detection and selective field freshness are deferred.

## Boundaries to revisit

The implementation intentionally has no Redis, vector database, browser pool, autonomous
shell, agent framework, graph database or distributed workflow service. Each would need
evidence that it solves an observed limitation better than the present adapter boundaries.
The strongest observed remaining gap is long-document evidence selection. Larger source
streaming, identity semantics, explicit refresh policies and storage/concurrency changes
remain contingent on representative workload measurements; the 0.3 batches address bounded
JSON/CSV processing rather than proving those broader capabilities.
