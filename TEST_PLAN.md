# Verification plan and acceptance evidence

Run `uv run pytest -q`, `uv run ruff check src tests scripts`, and
`uv run ruff format --check src tests scripts`. Build artifacts with `uv build`.
Recorded results are in `docs/VALIDATION.md`.

| Risk | Meaningful verification |
|---|---|
| Result/provenance corruption | Real HTTP pagination, exact source values, evidence links, immutable sightings |
| Duplicate state on rerun | Same idempotency key emits no requests; new key reuses assertions and adds sightings |
| Stale canonical data | Modified source changes the current candidate while preserving the old assertion |
| Conflicting sources | Two sources for one entity produce two candidates and no silently chosen canonical value |
| Crashed worker | Expired lease is reclaimed; stale worker cannot finish or charge budget |
| Partial transaction | Acquisition checkpoint survives; SQLite fault injection rolls back assertions, revision outcome and extraction completion together |
| Offline replay | No fetcher/model client; zero requests; old job exports stable; original capture/time retained; revised interpretation without false source conflicts |
| Evidence loss | Malformed/unsupported bytes survive parsing failure; abrupt post-checkpoint process exit does not require another GET |
| Migration | Schema-1 evidence, keys and pending reasoning survive; DDL rollback/restart and concurrent startup; migrated backup restored |
| Stale interpretation | Failed newer refresh/replay retains old candidates with explicit stale/attempt state; old replay does not supersede newer retrieval |
| Abrupt process death | Kill a subprocess inside a write transaction; restart, recover lease, check integrity |
| Concurrent claims/schedules | Multiple threads cannot claim one job or create duplicate next generations |
| Cancelled jobs | Existing token can no longer commit results or charge budget |
| Delayed retries | A long Retry-After cannot keep an elapsed job active |
| Access restrictions | Robots checked on redirected destination; forbidden target never contacted |
| SSRF / DNS rebinding | Private/metadata addresses rejected; socket connects to validated numeric IP |
| Trusted proxy boundary | Exact hostname allowlist excludes subdomains and unlisted destinations |
| Resource exhaustion | Request budget includes robots, body caps stop persistence, bounded gzip decompression rejects bombs |
| Model costs | Budget reserved before provider request; insufficient budget emits no billable call |
| Model fabrication / injection | Missing quote rejected; unknown fields cannot modify operator policy |
| Autonomous research wiring | Objective-only search through real local HTTP and model endpoints creates follow-up queries and grounded observations |
| API boundary | Bearer auth, idempotency conflict, cursor validation, export and inert capture downloads |
| Backups | Restored backup preserves job state, observations and evidence bytes |

The local fixtures are controlled test sources, not evidence of general web extraction or
model reasoning quality. Separate public-network acquisition validates real TLS/robots/HTTP
and actual document parsing. Optional providers and new adapters need their own corpus tests.

Run `uv run pytest tests/test_workloads.py -q -s` for the owned-source four-mode probes.
Before/after results and remaining failures are in `docs/validation/v02-workloads.md`.
Opt-in public verification: `uv run python scripts/verify_public.py --db data/new-public-check.sqlite`.
Use normal `HARVEST_*` network policy configuration; the script does not read ambient proxy
credentials or disable TLS. It contacts the public HTTPX GitHub metadata endpoint only.

Before production deployment beyond a single trusted operator, require: actual Docker image
and Compose smoke test, chosen model/SearXNG provider integration, prolonged mixed-workload
soak test, backup restore drill on target hardware, disk pressure behavior, dependency/image
review, benchmarked extraction/identity/contradiction quality and workload-derived limits.
The current suite does not substitute for those missing deployment checks.
