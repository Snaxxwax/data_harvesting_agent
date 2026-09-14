# Data model

Schema version 3 is in `src/harvest/store.py`. The immutable schema-1 bootstrap is followed
by a transactional migration that backfills extraction membership for legacy captures and
model sightings. Unknown versions fail startup. Timestamps are UTC Unix seconds.

The schema-2 -> 3 migration adds extraction batch progress and job processing counters.
Legacy record counts stay NULL/unknown. Historical claim counters are initialized from
stored assertion memberships; this is a lower bound where the old parser emitted duplicate
claims. New batches charge emitted claims even when observation deduplication reuses a row.
The job record counter starts at zero for processing counted by 0.3; it does not reconstruct
how many rows earlier versions visited. Per-extraction NULL values disclose that limitation.

| Table | Purpose and important invariants |
|---|---|
| `jobs` | Immutable spec snapshot/hash, idempotency key, state, budget counters, parent generation |
| `tasks` | Typed work, unique `(job, kind, key)`, parent lead, depth/reason, priority, retries, readiness, lease token |
| `events` | Append-only chronological audit of job decisions, captures, retries, budget reservations and limits |
| `blobs` | SHA-256 addressed permitted response bytes; identical captures share one body |
| `captures` | Unique task result, original/final URL, retrieval time/status/headers, raw hash, prior capture and changed flag |
| `extractions` | One deterministic interpretation attempt per leased task; job, original capture, extractor, outcome and processing timestamps |
| `assertions` | Observation membership in an extraction revision; optional reasoning adds assertions to that revision |
| `entities` | Dataset-scoped deterministic identity key; no implicit fuzzy merge |
| `observations` | Immutable source assertion, typed JSON value, evidence and extraction metadata |
| `sightings` | Historical union of observation/capture support across interpretations; not the authoritative current-revision view |
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
when the structured values did not change. Replay adds an extraction revision and memberships
without duplicating captures or changing their retrieval times. Job results join through
`assertions`, not capture ownership, so old job exports do not gain later replay assertions.
`observations.extraction_ids` in API/JSONL output identifies supporting revisions.

Extraction outcome is `complete` or `partial` after a successful deterministic commit;
pending/running/failed/blocked/cancelled state comes from its owning task. Reasoning has its
own task state and shares the deterministic revision. Capture `extractor` is a legacy field:
new source captures use `acquisition/1`; consult extraction records for actual parser versions.
Jobs distinguish `execution=online` from `offline_replay`. `captures` counts new acquisitions;
`evidence_captures` also counts captures referenced by replay. `extraction_progress` reports
interpretation states. Replay IDs and network prohibition are recorded in `replay_created`.

`extractions.records_processed` is the durable record ordinal for native JSON/CSV batches.
It counts visited records, including non-object entries skipped with warnings, not distinct
entities. `records_total` and computed `records_remaining` stay NULL when unknown; CSV learns
its total at EOF. `batches`, `batch_format`, `had_warnings` and `novel_claims` retain restart
state. Outcome stays NULL until the final batch. An unfinished/failed revision's assertions
remain in the owning job's export, but are excluded from the current-value selection below.
`jobs.records_processed` and `claims_processed` enforce aggregate processing limits across
captures, pagination, and (for claims) model and custom-adapter output.

Evidence locators are JSON Pointers for JSON, row/field locators for CSV, CSS/script
locations for HTML/JSON-LD, and offsets in normalized text for model extraction. The full
captured body is available independently. Model offsets refer to the stored extractor's
normalization procedure, not the original byte offset; the exact supporting quote is also
retained. Extractor versioning is required whenever that behavior changes.

Since 0.4, `model_reserved` events retain exact `system_prompt`/`user_prompt`, task attempt,
capture/extraction IDs, body hash, normalizer name and a `selection` object. Its original
normalized-text hash, selected/omitted character counts, selector/backend version and spans
make the input inspectable even after an adapter upgrade. Each span has original `start/end`
and selected-input `input_start/input_end`, all Unicode code points with exclusive ends.
`model/2` locators refer to original adapter text, not the concatenated selection. A quote
must fit one selected contiguous range. Prompt hashes cover system plus user message strings.
Earlier audit events do not gain invented prompt text. Schema remains 3; no backfill is needed.
Events describe reservations/attempts, not confirmation that the provider executed a request.

## Current value view

For a given entity and field, select assertions in each source's latest usable extraction,
ordered by capture retrieval time, capture ID, then extraction ID. `complete` and `partial`
interpretations are usable; partial means known omissions, not full coverage. Pending/failed
attempts cannot silently replace successful values. `source_states` and candidate `stale`
flags disclose newer attempts not represented in those values. This is extraction staleness,
not an age-based freshness SLA or evidence that an unreachable source remains current.
If these have one distinct typed value, expose it with all supporting candidate references.
If values differ, set `conflict=true` and `value=null`, retaining every candidate. Absent
fields are unknown, not empty or false. Old captures remain queryable through their
observations. A missing source refresh does not prove that an entity was deleted.

Extraction confidence is not a posterior truth probability. Source independence, authority,
temporal validity, units, jurisdiction and corroboration are not yet modeled sufficiently
for a defensible aggregate confidence score.
