# Evidence-driven next work

## Delivered

0.1 established permitted HTTP acquisition, structured assertions, provenance, scoped
discovery, bounded model proposals, recurring refresh, leases, budgets, API/CLI and backup.

0.2 revises the original transaction boundary: acquired source evidence survives parser
failure; deterministic extraction is separately resumable; offline replay produces new
interpretations of original captures without contacting sources. Current-value views expose
failed newer extraction attempts, and schema-1 history migrates without replacing evidence.
See [ADR 0002](docs/adr/0002-durable-acquisition-and-replay.md) and
[four-mode results](docs/validation/v02-workloads.md).

0.3 removes the native JSON/CSV 100-record cap through resumable record batches with
job-wide processing limits, record progress and atomic cursor checkpoints. The original
register now yields 250/250 entities; both 5,000-row JSON/CSV probes yield all entities and
25,000 observations. See [ADR 0003](docs/adr/0003-resumable-record-extraction.md) and
[0.3 evidence](docs/validation/v03-workloads.md). These are owned-source results, not
proof of coverage for arbitrary discovered populations.

0.4 selects bounded passages across adapter text, preserves exact model input and original
quote coordinates, and fixes sample-dependent gap tracking. The original long-report fact
is now extracted with unchanged request/call counts. Synthetic input coverage is 12/16;
see [ADR 0004](docs/adr/0004-bounded-document-evidence.md) and
[0.4 validation](docs/validation/v04-workloads.md).

0.5 adds durable multi-pass reading. Six requested fields at six distant offsets reached
3/6 in one pass (three ranked slots after introduction/conclusion) and 6/6 in two passes with
the unchanged selector, no new dependency and no schema change. Semantic retrieval was
measured only with static vectors, whose apparent gain was an anomaly-detection artifact on
the synthetic corpus; it is deferred pending a representative corpus and a runnable
transformer model, not rejected. See [0.5 validation](docs/validation/v05-reading-passes.md).

## Observed priorities, not a feature checklist

1. **Coverage after retrieval.** Multi-pass reading resolves the slot ceiling; unfamiliar
   vocabulary remains unmeasured because the synthetic corpus cannot distinguish semantic
   retrieval from anomaly detection. Build a labeled corpus of real documents with varied
   distractor prose, then compare FTS5 against a transformer embedding model in an
   environment with model access. Rereads add model calls per capture; an actual configured
   provider evaluation is still required before research-quality claims.
2. **Target identity and contradiction usefulness.** Source-local IDs and model claims
   attached to documents do not form a resolved target dossier. Measure exact-identifier
   reconciliation and cross-source evidence recall before fuzzy merges or graph infrastructure.
   Conflicts are preserved only when entity keys already match.
3. **Extraction diversity and larger sources.** Native JSON/CSV now processes bounded full
   responses, but HTML/JSON-LD retains its old caps, nested unknown JSON wrappers are not
   inferred, and custom plugins remain single-call adapters. The 5,000-row measurements
   do not justify unbounded JSON buffering. Adopt a streaming parser or external object
   storage only when representative source/memory measurements require it.
4. **Refresh semantics and operations.** New extraction staleness flags do not solve HTTP
   disappearance, entity deletion, partial snapshots, age-based freshness, or schedule-wide
   cost ceilings. Add explicit source-state/deletion rules only against labeled refresh cases.
   Soak, disk pressure, retention and target-host restore tests remain deployment work.

The ordering is revisable. The new probes give known ground truth but cover only four
owned-source scenarios. Broader source/format diversity and a real model/SearXNG evaluation
may expose a higher-leverage limitation. No distributed queue, graph database or autonomous
code execution is currently justified by these measurements.

## External state

Canonical repository: [Snaxxwax/data_harvesting_agent](https://github.com/Snaxxwax/data_harvesting_agent).
The earlier missing-repository blocker is resolved. The published 0.1 CI run passed tests
and Docker build. Compose deployment and live model/SearXNG quality remain unverified.
