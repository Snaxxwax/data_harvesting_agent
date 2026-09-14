# Durable multi-pass reading — 2026-09-13

Baseline: 0.4 commit `c284ca9`. Retrieval alternatives were measured first; results and
controls are in the repository history under the 0.5 spike (static vectors reproduced a
query-free anomaly ranking exactly: 13/16 with or without a query; target window ranked
first for the query "banana"). Semantic retrieval is therefore deferred, not rejected.

| Real owned-source HTTP workload (`tests/test_reading_passes.py`) | Result |
|---|---|
| Six fields at offsets 17k–77k in a 95k text document, `reading_passes=1` | completed, 1 model call, 3 missing |
| Same document, `reading_passes=3` | completed, 2 model calls, 1 GET, 0 missing; pass 2 spans disjoint from pass 1; all six locators at original offsets; rerun issues no request or call |
| Scripted reader returns no claims | 1 model call; rereading stops on zero novelty |
| 6,000-character document fully shown in pass 1 | 1 model call; `coverage_exhausted` recorded, no provider call for pass 2 |
| Reader repeats the same boilerplate claim, `reading_passes=2` | exactly 2 model calls |
| Restart between pass 1 and pass 2 | resumes, 2 calls total, 1 GET |

Full suite: 113 passed. `scripts/benchmark_passages.py` totals unchanged (12/16 single pass).
No dependency, schema or selector-ranking change. Known limitation: `missing_fields` is
job-wide, so rereads on a capture stop once any source supplies the field.
