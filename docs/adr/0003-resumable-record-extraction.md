# ADR 0003: bounded record batches within an extraction revision

Date: 2026-09-13. Status: implemented in 0.3.

## Observed problem

0.2 retained all source evidence, but extracted only 100 of 250 register rows. The
observation cap also discarded fields after 5,000 assertions in one response. These
were extraction boundaries, not source access or missing-data problems. Offline replay
now lets a better parser recover the same captured population without acquiring it again.

## External implementations and decision

Re-inspected [ijson](https://github.com/ICRAR/ijson), [Python CSV](https://docs.python.org/3/library/csv.html),
and [dlt performance controls](https://dlthub.com/docs/reference/performance) on the decision date.
ijson offers incremental object/event interfaces and a benchmark command. Its default
decimal values and optional float/backend behavior require compatibility work for our typed
assertions and large integer identifiers. It does not itself provide a durable cursor or
atomic observation checkpoint. It remains appropriate if measured source sizes require it.

Python's CSV iterator already handles quoted delimiters and embedded newlines; explicit
newline handling, strict parsing and unique headers prevent lossy dictionary conversion.
dlt's bounded extract-file and load-job sizes reinforce the value of limiting processing
units independently of entire source populations. Reuse those patterns, not a new loader.

Keep the standard JSON parser for the present 2 MB default / 20 MB maximum response ceiling.
Parse once per lease attempt, then iterate 50-record batches. CSV rows are read iteratively
from the retained representation. Healthy execution does not reopen/reparse for every batch.
A reclaimed lease reparses once and skips the committed record ordinal. This avoids both
a second record-staging store and quadratic reparsing in the normal path. JSON remains
buffered in memory; this is not a streaming-JSON or arbitrarily large-source implementation.

The 5,000-row owned JSON and CSV workloads recovered every entity, each with 25,000
observations, in 100 batches. Acquisition/extraction took about 1.2 seconds in this workspace;
full-process peak RSS was about 128 MB including fixture construction and export comparisons.
These measurements justify retaining the parser at this scale, not a universal memory bound.

## Persistence and limits

- Each batch commits assertions, global counters, extraction progress and discovered leads
  in the same lease-fenced transaction. Parser/DB failures cannot advance a cursor without
  its data. The task remains leased across batches; task/attempt quotas do not grow per batch.
- `limits.records` defaults to 10,000 structured rows per job; `limits.claims` defaults to
  100,000 candidate assertions across all extraction methods. Repeated/deduplicated claims
  still consume processing budget. Record limits include skipped non-object array entries.
- Claim budgets commit whole batches; the last fitting batch survives, and unused quota
  may be less than one batch. Record budgets can stop on a shorter final batch. Terminal
  budget exhaustion is recovered with a new replay; active crashed jobs resume their cursor.
- JSON array/object totals are known once a batch commits. CSV totals remain unknown until
  EOF, rather than guessing from physical line counts. Processed rows are not entity recall:
  duplicates, skipped records, unknown wrappers and missing fields still matter.
- Until EOF, the revision has no completed outcome. Its committed assertions remain
  inspectable in job exports but do not replace the canonical source interpretation.
  Previous usable values remain visible with stale-attempt flags. Final warnings produce
  a usable partial interpretation, as in 0.2; field omissions remain explicit limitations.
- Preserve existing JSON/CSV observation identities and locator semantics. The durable
  cursor format is separately identified by `batch_format`; changed body hashes are rejected.
  Existing trusted plugin subclasses retain their `extract` override behavior.

## Migration and remaining bounds

Schema 2 -> 3 adds progress and processing counters transactionally. Legacy record counts
remain NULL/unknown, and legacy claim counters are initialized from stored assertion
memberships (a lower bound if the old extractor emitted duplicates). Existing online and
replay idempotency keys remain valid after adding default limits. Migrations from schema 1
run 1 -> 2 then 2 -> 3, each transactionally. Mixed-version workers are unsupported.

This does not remove HTML/JSON-LD's legacy record caps, 100 fields per structured record,
20,000-character evidence-value limits, request/body/frontier budgets, or the model's
14,000-character source window. Custom plugins still use their existing bounded single-call
contract. Population definition, semantic entity reconciliation, source deletions and
long-document evidence selection remain separate problems.
