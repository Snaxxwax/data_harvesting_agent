# Source audit and reuse decisions

Researched 2026-09-12 using repository source, official docs and installed package metadata.
Statements labelled **verified** describe inspected material. **Decision** paragraphs are
engineering judgments, not claims that alternatives are unsuitable in general. No broad
performance benchmark or exhaustive issue/security audit was completed.

## Candidates inspected

| Component | Verified evidence | Decision for this milestone |
|---|---|---|
| HTTPX / HTTPCore | BSD-3-Clause; installed 0.28.1 / 1.0.9. HTTPX transport source delegates HTTP connections to HTTPCore; custom transports and network backends are documented. | Reuse for deterministic HTTP, pooling, TLS and streaming. Add only destination validation and capture policy. |
| Protego | BSD-3-Clause, installed 0.6.2. Official parser documentation exposes wildcard rules, length precedence, crawl delays and request rates. | Reuse for robots parsing; wildcard-denial regression test exercises the acquisition boundary. |
| Crawlee Python | Apache-2.0, active repository (pushed 2026-09-10). SQL queue source has blocking/claim state and PostgreSQL `skip_locked`, plus separate handled/reclaim operations. | Strong future crawling adapter. Do not make its independently committed queue the authority for our evidence transaction. |
| Scrapy | Official job docs persist scheduler/duplicate state and explicitly warn that unclean shutdown can corrupt the job directory. | Candidate for mature source-specific crawling. `JOBDIR` alone does not satisfy the crash-recovery milestone. |
| Huey | MIT, repository pushed 2026-09-08. `SqliteStorage.dequeue` selects and deletes a task before returning it to the caller. | Do not use dequeue alone as the durable claim/ack protocol. Adding a second recovery ledger would duplicate this project's domain state. |
| LangGraph | MIT, repository pushed 2026-09-11; inspected retry source handles attempt counts, timeout/cancellation and backoff. | Defer framework adoption. A persistent typed proposal task is sufficient for the current research loop; revisit for complex graph/human-interrupt semantics. |
| SQLite | Official documentation describes transaction journaling, local-host WAL limitations, and the 2026 WAL-reset fix. Local runtime is SQLite 3.53.1. | Reuse transactional SQLite with rollback journal and `synchronous=FULL`; no network filesystems. Keep blobs in the transaction for now. |
| PostgreSQL / Psycopg | Official SELECT docs expose row locks/`SKIP LOCKED`; Psycopg documents transaction contexts. | Revisit for multi-host workers or measured contention. A database server adds no validated capability for the first one-host job. |
| Temporal | Official docs distinguish workflow service/self-hosting and visibility persistence. | Defer the operational service until durable distributed orchestration is actually required. No claim of feature equivalence. |
| SearXNG | Official API accepts search queries and selectable JSON; JSON must be enabled, and public instances often disable it. | Optional operator-hosted source discovery adapter. Do not scrape a search results UI or assume a free public endpoint. |
| LiteLLM | Official docs offer provider routing, gateways and spend tracking. | Optional external compatible gateway. No mandatory in-process dependency; provider endpoint contract is narrower and independently testable. |

## Exact inspected source references

- [HTTPX transport source at b5addb6](https://github.com/encode/httpx/blob/b5addb64f0161ff6bfe94c124ef76f6a1fba5254/httpx/_transports/default.py),
  [custom transport documentation](https://www.python-httpx.org/advanced/transports/),
  [HTTPCore network backends](https://www.encode.io/httpcore/network-backends/).
- [Crawlee SQL queue at fefd7b8](https://github.com/apify/crawlee-python/blob/fefd7b8157612ed4273d7723c7b329df8d1ecea8/src/crawlee/storage_clients/_sql/_request_queue_client.py),
  [SQL request queue API](https://crawlee.dev/python/api/class/SqlRequestQueueClient).
- [Huey storage source](https://github.com/coleifer/huey/blob/master/huey/storage.py),
  inspected `SqliteStorage.dequeue`, transaction and task-table definitions. This link
  tracks a branch; reverify before a future adoption decision.
- [LangGraph retry source at e539ac1](https://github.com/langchain-ai/langgraph/blob/e539ac122f4126f6dd850581c1494948cf620e31/libs/langgraph/langgraph/pregel/_retry.py).
- [Scrapy pause/resume limitations](https://docs.scrapy.org/en/latest/topics/jobs.html).
- [SQLite journaling and WAL-reset documentation](https://www.sqlite.org/wal.html),
  [PostgreSQL SELECT](https://www.postgresql.org/docs/current/sql-select.html),
  [Psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html),
  [Temporal documentation](https://docs.temporal.io/).
- [SearXNG search API](https://docs.searxng.org/dev/search_api),
  [LiteLLM SDK/gateway boundary](https://docs.litellm.ai/docs/learn/sdk_quickstart).
- [Protego parser API and conventions](https://github.com/scrapy/protego/blob/master/README.rst).

## Adopted dependency burden

Runtime pins: HTTPX 0.28.1, HTTPCore 1.0.9, FastAPI 0.141.1, Pydantic 2.13.5,
Beautiful Soup 4.15.0, Uvicorn 0.52.4, Protego 0.6.2. Installed metadata reports MIT for FastAPI,
Pydantic and Beautiful Soup, BSD-3-Clause for HTTPX, HTTPCore, Uvicorn and Protego. Transitive
versions and distribution hashes are in `uv.lock`; installed license metadata was
checked, not independently adjudicated. The application is MIT-licensed.

These dependencies provide HTTP, server/API, schema validation and parsing. They add
package updates rather than independently operated infrastructure. A gateway or SearXNG
adds a service only when configured. Browser engines, vector/search/graph stores and
distributed brokers add no present requirement and are deferred.

## Evidence that changed the initial approach

1. Queue inspection made the required transaction boundary concrete: evidence, observations,
   lead insertion and task completion need one atomic write. The frontier became domain
   state in the Store rather than a second general queue.
2. A forced database failure and a killed process verified rollback and lease recovery.
3. A live request exposed remote-DNS proxy and certificate-store constraints. The proxy
   adapter now requires an explicit public-host allowlist and uses verified system TLS.
4. Enumeration tests separated pagination from research depth. Response extraction caps
   now emit visible warnings and produce a partial status.
5. Deferred-retry tests exposed deadline handling outside ready work; the worker now expires
   elapsed jobs even while no task is eligible.

## 0.2 re-evaluation — 2026-09-13

Four-mode HTTP workloads showed that point 1 above coupled durability too tightly to
successful parsing. Already-downloaded malformed/unsupported bodies were lost. The
revised design checkpoints acquisition and extraction work first, then commits assertions
atomically with extraction completion. See [ADR 0002](docs/adr/0002-durable-acquisition-and-replay.md).

Re-inspected [dlt production retry behavior](https://dlthub.com/docs/running-in-production/running),
[Scrapy response caching](https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#module-scrapy.downloadermiddlewares.httpcache),
and [warcio capture code](https://github.com/webrecorder/warcio/blob/master/warcio/capture_http.py).
dlt's pending-data processing informed staged durability. Scrapy caching alone does not
provide this platform's interpretation/job lineage. warcio's `http.client` interception
would not directly cover the policy-pinned HTTPX transport. WARC interchange remains a
possible future adapter. No new dependency or service was needed for the selected milestone.
