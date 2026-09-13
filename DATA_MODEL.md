# Data model

Schema version 1 is in `src/harvest/store.py`. Unknown schema versions fail startup.
Future schema changes must use explicit tested migrations; adding application code alone
is not a migration strategy. Timestamps are UTC Unix seconds.

| Table | Purpose and important invariants |
|---|---|
| `jobs` | Immutable spec snapshot/hash, idempotency key, state, budget counters, parent generation |
| `tasks` | Typed work, unique `(job, kind, key)`, parent lead, depth/reason, priority, retries, readiness, lease token |
| `events` | Append-only chronological audit of job decisions, captures, retries, budget reservations and limits |
| `blobs` | SHA-256 addressed permitted response bytes; identical captures share one body |
| `captures` | Unique task result, original/final URL, retrieval time/status/headers, raw hash, prior capture and changed flag |
| `entities` | Dataset-scoped deterministic identity key; no implicit fuzzy merge |
| `observations` | Immutable source assertion, typed JSON value, evidence and extraction metadata |
| `sightings` | Many-to-many link from observations to retrieval captures, enabling history and freshness |
| `origin_state` | Shared per-origin next-request time across workers |
| `robots` | Cached robots policy and expiry; failures are not cached as permission |
| `schedules` | Durable refresh interval, next run, last job and enabled state |

## Identity

Entity IDs hash the dataset and identity key. Absolute identifier/entity URLs are preferred.
Otherwise the source origin and collection path namespace an explicit source-local ID.
Without a usable ID, an exact record fingerprint prevents speculative merging. Changing
an unidentified record can therefore create a new entity; this is a documented limitation.
Aliases and relationships are preserved as source fields and can supply acquisition leads.
They do not yet form a resolved graph with merge/split operations.

## Observations and history

An observation ID hashes entity, field, typed value, source URL, evidence, locator, method,
extractor version and extraction confidence. Rerunning identical extraction reuses the
observation; a new sighting records its new capture time. Different values, evidence, or
extraction versions remain separate. Raw body hash comparison emits a content change even
when the structured values did not change.

Evidence locators are JSON Pointers for JSON, row/field locators for CSV, CSS/script
locations for HTML/JSON-LD, and offsets in normalized text for model extraction. The full
captured body is available independently. Model offsets refer to the stored extractor's
normalization procedure, not the original byte offset; the exact supporting quote is also
retained. Extractor versioning is required whenever that behavior changes.

## Current value view

For a given entity and field, select observations sighted in each source's latest capture.
If these have one distinct typed value, expose it with all supporting candidate references.
If values differ, set `conflict=true` and `value=null`, retaining every candidate. Absent
fields are unknown, not empty or false. Old captures remain queryable through their
observations. A missing source refresh does not prove that an entity was deleted.

Extraction confidence is not a posterior truth probability. Source independence, authority,
temporal validity, units, jurisdiction and corroboration are not yet modeled sufficiently
for a defensible aggregate confidence score.
