# Resumable record workload evidence — 2026-09-13

Baseline: 0.2 commit `097b2e0d3fc5a06d28caf892d207a813d5aefae8`.
The prior 61-test suite and four-mode evidence are retained in Git history. The 0.3 tests
change the register expectation because actual execution now recovers the full fixture.

| Case | 0.2 | 0.3 |
|---|---|---|
| 250-row, six-column CSV register | partial; 100 entities / 600 observations | completed; 250 entities / 1,500 observations |
| Requests for that register | 2 (robots + source) | 2, unchanged |
| JSON and CSV, 5,000 rows each | not run as 0.2 benchmarks | 5,000 entities / 25,000 observations each; 100 committed batches |
| 51 JSON records, 100 fields each | 5,000-assertion response cap | 5,100 assertions, completed |
| 250 rows with 125-record budget | unavailable processing quota | 125 rows retained, budget exhausted; JSON remaining=125, CSV remaining unknown |
| Crash after first 50-record commit | no record cursor | resumes to 250 rows without duplicate accounting or another GET |
| Late malformed CSV / incomplete refresh | whole extraction failed | prior batches remain inspectable; unfinished revision does not replace current values |

The targeted malformed-export, continuous schema-break, and Deep Research mixed-evidence
probes still pass with the 0.2 evidence-retention behavior. The research accreditation fact
beyond character 14,000 still fails to reach the model. This milestone improves structured
coverage and recovery; it does not establish semantic research quality.

## Larger owned-source runs

Reproduce using a new DB for each command:

```bash
uv run python scripts/benchmark_batches.py --db data/json-benchmark.sqlite --format json
uv run python scripts/benchmark_batches.py --db data/csv-benchmark.sqlite --format csv
```

| Measurement | JSON | CSV |
|---|---:|---:|
| Source representation bytes | 516,687 | 186,713 |
| Acquisition + extraction seconds | 1.151 | 1.211 |
| Offline replay seconds | 0.830 | 0.863 |
| Source requests including robots | 2 | 2 |
| Offline replay requests | 0 | 0 |
| Whole-process peak RSS bytes | 127,614,976 | 127,442,944 |
| Database bytes after acquisition + replay | 22,847,488 | 22,429,696 |

Each run verifies 5,000 entity keys, 25,000 assertions, capture/extraction references,
identical observation identities on replay, no extra source requests, database integrity and
foreign keys. The memory measurement includes generated fixture data, loaded observation
exports and replay comparisons. These single-host results are not a scale/soak test or a
parser-only memory measurement. JSON is still parsed in memory within the body-size ceiling.

## Migration and semantics

The real schema-2 acceptance DB was copied before upgrade. All immutable capture, observation,
sighting, blob and schedule hashes matched afterward. All four keys, including offline replay,
continued returning their original jobs after default processing limits were added. The
original remains schema 2. See [v03-results.json](v03-results.json).

An unfinished revision has no completed outcome even when its job export contains durable
rows. This prevents a short processed prefix from deleting values in the current source view.
Terminal budget stops require a new replay with a larger budget; interrupted active jobs
resume their saved cursor. Field/format omissions still produce explicit partial results.
CSV totals are learned only at EOF; processed rows do not prove unique-entity recall.
