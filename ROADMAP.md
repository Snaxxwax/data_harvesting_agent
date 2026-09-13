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

## Observed priorities, not a feature checklist

1. **Bounded structured-population coverage.** The 250-row owned register yields 100
   entities, although all requested field names are present. Implement resumable extraction
   batches with explicit processed/remaining counts and global assertion/resource limits.
   Evaluate streaming JSON/CSV libraries against the existing 20 MB response ceiling before
   adopting one. Use the new replay path to compare implementations on identical evidence.
   Do not simply raise an arbitrary cap and label enumeration complete.
2. **Document evidence selection.** Deep Research missed an explicit accreditation fact
   beyond character 14,000. Compare deterministic passage selection and bounded multi-pass
   extraction against a labeled long-document corpus. Preserve exact supporting passages
   and account for additional model cost. Test an actual configured provider before making
   research-quality claims; current model fixtures test contracts, not intelligence.
3. **Target identity and contradiction usefulness.** Source-local IDs and model claims
   attached to documents do not form a resolved target dossier. Measure exact-identifier
   reconciliation and cross-source evidence recall before fuzzy merges or graph infrastructure.
   Conflicts are preserved only when entity keys already match.
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
