# Historical exposure search

Harvest can import normalized exposure metadata and search every retained revision by
an exact email, international phone number, or username within a named source namespace.
The local archive complements future continuous monitoring: an old breach can be queried
even after its original page disappears. No external breach provider is connected by this
feature, and generic harvested observations are not automatically treated as breaches.

## Import and search

Use records you are authorized to process. The supplied example is entirely synthetic:

```bash
harvest import-exposures examples/historical-exposures.jsonl --dataset exposure-demo
harvest search-exposures examples/historical-exposure-search.json
```

Each JSONL line is one `ExposureRecord`, as shown in the example. The CLI streams batches
of 500, with a 64 KiB line limit. A failed batch rolls back; earlier batches remain committed.
Correct the input and rerun it: identical normalized records are deduplicated. Changes to
content or acquisition time produce a new historical revision. Preserve original acquisition
timestamps when retrying. Different sources remain separate evidence; result counts are
counts of record revisions, not unique breaches or affected people.

The bearer/session-authenticated API offers:

- `POST /exposures/import`: `{"dataset":"exposure-demo","records":[...]}`; 1–500 records,
  validated before the batch is committed atomically.
- `POST /exposures/search`: the same JSON as the search example. Optional `breach_id`,
  `source`, and `evidence_level` filters, `limit` (1–200), and `after` (the previous page's
  `next_after`).
- MCP: `search_historical_exposures(dataset, kind, value, namespace?, source?,
  evidence_level?, after?, limit?, breach_id?)`. This calls the authenticated search endpoint.

Search uses a POST body to keep identifiers out of URL/access-log query strings. Responses
are marked `Cache-Control: no-store`. Search returns exposure categories and provenance;
it does not return the other identifiers associated with a matching record.

## Evidence contract

Required fields: `source`, `source_record_id`, HTTP(S) `source_url`, `breach_name`,
`evidence_level`, nonempty `identifiers`, `evidence_ref`, and timezone-aware `acquired_at`.
Optional `breach_date` and timezone-aware `published_at` remain distinct from `indexed_at`,
which Harvest records at import. A source URL is a reference, never fetched during import.
An optional `breach_id` links the record to an existing catalog entry. Unknown catalog IDs
are rejected atomically. Records imported without a catalog ID retain their original
fingerprints and remain searchable; they are reported as unlinked in the coverage matrix.

`evidence_level` is an importer assertion:

| Value | Meaning |
|---|---|
| `record_observed` | Importer observed the identifier in a source record |
| `provider_reported` | Provider reports exposure or breach membership |
| `publisher_claim` | Publisher claims exposure; record contents are unconfirmed |

`observed_fields` describes fields actually present in that individual's record and is
only accepted for `record_observed`. `reported_fields` describes claimed/reported data
categories, potentially for the breach as a whole. Categories are `email`, `username`,
`phone`, `name`, `address`, `date_of_birth`, `password`, `password_hash`, `session_token`,
`financial`, `government_id`, and `other`. These are field **names**, not leaked values.
Use `evidence_ref` for a row ID, provider result reference, or restricted evidence locator.
Do not put raw credentials or sensitive record contents in metadata strings. Arbitrary
fields such as `password` or `raw_record` are rejected by the import schema.

Email domains are normalized to lowercase/IDNA. Local-part case, dots, and plus tags are
preserved; enroll known variants explicitly. Phone values require `+` followed by country
code and digits. Usernames are case-sensitive and require a lowercase `namespace`, such
as `forum-a`. No fuzzy name matching, alias inference, or cross-site username merging.

## Breach catalog and coverage registry

The catalog stores incident metadata independently of personal exposure records. Import
the starter public metadata and provider declarations, then inspect the matrix:

```bash
harvest import-breach-catalog examples/breach-catalog.json
harvest import-breach-coverage examples/breach-coverage.json
harvest breach-coverage examples/breach-coverage-query.json
```

The starter has four entries inspected in HIBP's public metadata API: Adobe (2013),
LinkedIn (2012), Verifications.io, and Collection #1. These are a small starter set, not
the complete UpGuard list. The coverage file uses dataset `exposure-demo`; change it to
the intended local dataset. Importing these files creates **no individual exposure records**.
See the [starter coverage matrix](breach-coverage-matrix.md) and its source references.

Catalog input is `{"entries":[...]}`. Each entry has a stable `id`, `name`, a `kind`
(`incident`, `exposure`, `scrape`, `compilation`), and at least one reference with a URL,
publisher, role (`primary`, `provider`, `secondary`), and timezone-aware `checked_at`.
Optional `breach_date`, `disclosed_date`, source-named `reported_fields`, and `counts`
retain incident context. A count requires a value, unit (records/accounts/people/email
addresses/passwords/documents), status, and cited reference. Compilations cannot carry a
single breach date. Publication, provider addition, and breach dates must not be conflated.

Coverage input is `{"dataset":"...","entries":[...]}`. Each entry links a `breach_id`
to a `source`, provider-declared `availability` (available/unavailable/unknown), supported
`identifiers` (kind and optional username namespace), `evidence_detail`
(membership/field_categories/record_fields/unknown), and a checked `reference`.
These declarations describe provider listings, not connector readiness or subscription
entitlements. No provider connector is installed by this feature. A check older than
30 days is flagged `check_stale`.

The API and MCP offer:

- `POST /breach-catalog/import`: import up to 500 catalog entries atomically.
- `POST /breach-coverage/import`: import up to 500 declarations atomically, scoped to a
  dataset. Unknown breach IDs reject the entire batch.
- `POST /breach-coverage/search`: accepts `dataset`, `kind`, optional `namespace`,
  `breach_id`, `source`, `evidence_level`, `after`, and `limit` (1–200).
- MCP `get_breach_coverage` takes the same coverage-query fields.

Both catalog and declaration edits retain database revisions. Identical reimports are
idempotent and cannot revert a later revision. New differing imports become current, so
operators should check source dates before importing older material. The catalog is shared
deployment metadata; coverage declarations and local counts are scoped to datasets.

Coverage is computed for the requested identifier kind, namespace, source, and evidence
filter. Only linked local records make `searchable_now` true. Provider declarations cannot
set it. Statuses are `local_records_available`, `provider_not_connected`,
`records_unavailable`, `coverage_unknown_or_unsupported`, and `metadata_only`.
Even when records are searchable, completeness remains unknown.

Historical search now includes a bounded `catalog_coverage` page with per-incident outcomes:

| Outcome | Interpretation |
|---|---|
| `matched` | An imported record contains the exact identifier |
| `no_match_in_imported_records` | Relevant local records exist, but none contain this identifier |
| `not_searched_no_local_records` | No local records support this lookup; no negative conclusion |

These outcomes consider all matching records, independently of exposure-result pagination.
The nested coverage page has its own catalog-ID `next_after`, distinct from exposure IDs.
Use the coverage endpoint to page through remaining incidents; use a `breach_id`-filtered
exposure search for a specific incident's match outcome. Unlinked local records remain in
unfiltered exposure searches and are counted separately; names are never automatically
merged into catalog IDs. A catalog article alone must not be imported as personal exposure.

## Coverage and operational limits

- Results explicitly say `coverage: local_imports_only`. An empty result does not mean
  that a person has never been exposed. No paid provider, historic corpus, automatic
  backfill, monitoring subscription, or alert delivery is installed by this feature.
- Exact identifier overlap does not independently verify a person's identity, the source's
  truthfulness, or whether a credential is currently usable. Evidence labels and source
  dates are supplied by the importer. No credential testing takes place.
- Search includes all revisions and uses stable ID ordering, not chronological ranking.
  New records added during pagination may require a fresh search to see every addition.
- Import requires normalized metadata. It does not parse arbitrary SQL dumps, archives,
  or raw credential files. Source-specific adapters must produce this contract.
- The tables live in Harvest's existing SQLite database and are covered by its database
  backup. This remains a trusted-operator installation: dataset names are organizational
  partitions, not per-user access controls. Existing limits on encryption at rest,
  retention, and multi-tenant isolation still apply. Identifiers are stored in plaintext.

Next integration: populate the same record contract from an authorized provider and
source adapters, then use it for both historical baseline checks and new-record matching.
