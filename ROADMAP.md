# Evidence-driven next work

## Delivered in 0.1

- Research audit and initial architecture decisions.
- Real HTTP to structured observations and content-addressed evidence.
- Idempotent submission, persistent retries, lease recovery and atomic result commits.
- Authenticated API/CLI, progress/events/export, backups and deployment configuration.
- Deterministic JSON/CSV/HTML/JSON-LD, bounded gzip/deflate, pagination and exact identifiers.
- Optional objective-only discovery and recursive model proposal loop.
- Continuous refresh generations with conditional requests and source-change history.
- Fault injection, process death, concurrency, budget and security verification.

## Strongest next step: evaluate useful research, not add infrastructure

Build a versioned evaluation corpus around three representative jobs: organization
investigation, an enumerated public register, and continuous project/product monitoring.
Label expected identifiers, field values, supporting evidence, known contradictions,
known omissions and refresh changes. Run the chosen local or hosted model and SearXNG
configuration against it. Measure factual precision, population recall where ground truth
exists, false merges, citation entailment, independent-source coverage, cost and elapsed time.

Use failures to prioritize the next implementation increment:

1. **Source and schema planning:** typed source capabilities, proposed target schema,
   explicit completeness assumptions, larger evidence context selected deterministically.
2. **Extraction coverage:** streaming JSON/CSV and document adapters; test Crawlee wrapping
   only when a measured task actually requires JavaScript rendering. Preserve the current
   policy and commit boundary.
3. **Entity reconciliation:** normalized exact identifiers, namespace declarations, aliases
   and relationship records, conservative match candidates, auditable reversible merge/split.
4. **Research quality:** explicit contradiction sets with evidence references, source
   independence and authority signals, field-level uncertainty, validated information gain.
   Replace the metadata-novelty plateau only after a better measure has evidence.
5. **Maintenance:** source refresh state, per-field staleness, selective revisits, conditional
   request cache, deletion/tombstone policy and scheduled cost ceilings over all generations.
6. **Operations:** throughput/soak/disk-failure measurement, metrics and alerts, retention,
   configurable source policies and source authentication for authorized accounts.
7. **Scale when justified:** PostgreSQL transaction backend with task leases and concurrent
   workers; staged object storage; migration/rollback verification. Adopt a workflow service
   only if the operational benefit exceeds the extra infrastructure.

## Blocking external setup

GitHub is connected as `Snaxxwax`, but the available integration can write files/commits to
existing repositories and cannot create a repository. The code is maintained in local Git
pending an empty destination repository. No unrelated existing repository was repurposed.
No GitHub push, hosted deployment, or actual paid/local inference run is claimed.
