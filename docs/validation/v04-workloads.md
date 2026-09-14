# Bounded document evidence — 2026-09-14

Baseline: 0.3 commit `0d637c76d43274b23c997798d07dc1dd305a2822`.
Inspected source, history, tests, architecture, operations and prior validation; reran all
four owned-source mode probes before implementation. The baseline omission was reproduced,
not inferred solely from the roadmap. [ADR 0004](../adr/0004-bounded-document-evidence.md)
records the research, choice and limits.

| Real owned-source HTTP workload | 0.3 | 0.4 |
|---|---|---|
| Targeted directory + malformed supplier JSON | partial, 2 captures, 4 observations, 3 requests | unchanged |
| Enumerate 250-row CSV | completed, 250 entities, 1,500 observations, 2 requests | unchanged |
| Continuous initial JSON then malformed refresh | initial completed; refresh partial, old value explicitly stale | unchanged |
| Deep Research mixed primary evidence | partial, 5 captures, 4 observations; accreditation missing | partial, 5 captures, 5 observations; accreditation quote retained |
| Deep Research resource use | 9 requests, 3 scripted model calls | unchanged |
| Long plain-text/HTML report, targeted | prefix omitted distant fact | one model call; supported claim has original Unicode offset and exact input audit |
| Long-report continuous value change | not a prior probe | new value current; original assertion retained |
| Crash before model send | reservation without exact input | input persists before dispatch; restart retains both attempts, completes with no new source GET |

Deep Research remains partial because the mixed workload includes an unsupported supplier
format. The scripted reader only emits a claim when its literal quote reaches the prompt.
These HTTP runs verify input plumbing, persistence and output provenance, not model reasoning
quality or independent truth. No live model, live SearXNG, paid API or fresh public acquisition
was used. Historical public-source validation is preserved separately.

## Synthetic document comparison

Reproduce: `uv run python scripts/benchmark_passages.py`.
The corpus is ten generated 95,000-character documents with 16 explicit quote labels.
Facts occur at varied offsets; two are contradictory statements; one uses unmatched
vocabulary; one document has six competing fields. Results are in
[v04-passages.json](v04-passages.json).

| Strategy | Labeled quotes exposed | Source input bound |
|---|---:|---|
| Original prefix | 2 / 16 | 14,000 characters |
| Five evenly spaced samples | 2 / 16 | 13,000 characters |
| Ranked passages with introduction/conclusion | 12 / 16 | at most 14,000, including omission markers |

The ranking run selects 12,600–13,000 source characters per document. Both sides of the
distant conflict are included. The unmatched-vocabulary fact and three of six competing
fields are missed. This small synthetic corpus is deliberately diagnostic and includes
failures; it is not a representative web benchmark or an entailment score. Increasing model
intelligence alone cannot recover facts never supplied. Durable multi-pass coverage and
semantic retrieval remain candidates for the next measured comparison.

## Automated verification and implementation corrections

`tests/test_passages.py` covers varied offsets and Unicode, overlapping boundaries, distant
contradictions, field diversity, unchanged short inputs, literal FTS query handling,
deterministic selection, missing-FTS5 fallback, truncation warnings, exact model-input audit,
original capture identity, reruns, uncertain-attempt recovery, continuous refresh, cross-gap
quote rejection, corrupt capture rejection and sample-independent gap tracking.

Testing corrected the HTML fixture's assumptions: normalized HTML trims trailing whitespace
and identifies itself as `html-jsonld/1`. Audit checks now compare against those existing
semantics. Corpus inspection also exposed an undersized final window; it now anchors at the
document end to use the bounded input allowance. No dependency or schema change was needed.
Final suite/build results are in [test-summary.json](test-summary.json).

Scope remains one trusted operator/host. Docker runtime, provider quality, broad natural
document recall, long-running soak and disk-pressure behavior remain unverified. Prompt
audits increase stored data per attempt, and neither event retention nor model-response
replay is implemented. Completed means frontier execution, not comprehensive document review.
