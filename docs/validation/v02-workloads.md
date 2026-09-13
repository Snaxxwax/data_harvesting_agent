# Four-mode workload evidence — 2026-09-13

Baseline: published 0.1 commit `d66f3d33f5926ac50e277df8df83200eda7fedfd`.
Revised: 0.2 acquisition checkpoint / extraction revision implementation.
Reproduce with `uv run pytest tests/test_workloads.py -q -s`.

These are deterministic owned-source scenarios over real local HTTP, not third-party
production data. The search/model endpoints speak the actual integration contracts but
return scripted responses. The model reports the accreditation quote only if it is in its
input; that makes the document-window limitation measurable without claiming model quality.
No model spending or source-access bypass is involved.

| Workload | Observed 0.1 | Observed 0.2 | What this establishes |
|---|---|---|---|
| Targeted company investigation: directory + malformed supplier JSON | partial; 1 capture, 4 observations, 3 requests | partial; 2 captures, same 4 observations and 3 requests | Both acquired bodies survive; malformed data is not silently repaired |
| Enumeration: 250-row, six-column register CSV | partial; 100 entities, 600 observations, 2 requests | unchanged | 40% fixture population recall; empty missing-fields list is not completeness |
| Continuous: valid record then broken changed export | initial completed, 1 capture/2 observations; refresh failed with 0 captures, 1 request | initial unchanged; refresh partial with 1 capture, 1 request | Failed extraction retains new evidence; old employees value remains explicitly stale |
| Deep Research: objective search, index, register, unsupported export, long report | partial; 4 captures, 4 observations, 9 requests; accreditation missing | partial; 5 captures, same observations/requests; accreditation still missing | Unsupported primary material survives; 14k model window remains a real gap |

Capture counts for Deep Research include the search response. Each test starts with an
empty store, so counts include a robots request. Targeted and continuous malformed JSON
contain a trailing comma. The built-in parser correctly rejects it; recovery must not
invent a valid source representation.

## Useful recovery, not just retention

An additional owned supplier-format case first fails extraction on
`application/x-northstar-export`, retaining its body. Installing a deterministic adapter
for that documented fixture format and replaying the capture recovers `employees="45"`
with original evidence, no additional requests, and no changes to the failed job's output.
This test demonstrates the extension/replay path; it is not a new general document adapter.

Additional verification covers revised interpretations without false source conflicts,
old snapshot replay not superseding newer retrievals, pending/failed attempt disclosure,
lease fencing, cancellation, task-cap recovery, transaction rollback, abrupt process exit,
API/CLI replay, authentication, and no model/search/fetch tasks in offline execution.

## Real prior evidence and fresh-network limits

A copy of the actual 0.1 public-acquisition database migrated with all 89 observations,
173 sightings, three captures and three idempotency keys preserved. See
[the machine-readable upgrade report](v02-upgrade.json). The original remains schema 1.
Replaying its HTTPX capture under 0.2 recovered 84 observations, referencing the original
capture and retrieval timestamp, with zero requests and zero model calls.

A fresh public API run in this session failed after three timed-out attempts and received
zero bytes. It is not counted as validation success. The opt-in `scripts/verify_public.py`
remains available for environments with permitted public egress. The separate 0.1 public
results remain historical evidence; no fresh 0.2 public acquisition or live model quality
claim is made here.

## Decision and remaining priorities

[ADR 0002](../adr/0002-durable-acquisition-and-replay.md) records why evidence loss came
before extraction capacity or infrastructure. The revised boundary makes future adapters
testable against identical persisted evidence. Next priorities are bounded resumable record
extraction and document passage selection. No population-recall or research-quality gain
is claimed for this milestone beyond recovery of previously unusable acquired evidence.
