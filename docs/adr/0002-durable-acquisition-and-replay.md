# ADR 0002: acquired evidence survives extraction failure

Date: 2026-09-13. Status: implemented and verified in 0.2; supersedes ADR 0001's
single acquisition-and-extraction transaction, not its fencing or atomic-result rules.

## Evidence and priority

The four-mode probes in `tests/test_workloads.py` run actual HTTP against owned sources.
They reproduce a supplier's malformed export, a 250-row register, a broken refresh,
and objective-only research across mixed-format primary material. The model is scripted;
this is an integration/coverage probe, not evidence of model research quality.

On published 0.1 (`d66f3d3`): targeted acquisition retained only one of two bodies;
the failed refresh retained none of the changed body; research discarded an unsupported
supplier export. A 250-row register produced 100 entities (40% fixture recall), while
`missing_fields=[]`. A fact after character 14,000 never reached the model.

Priorities: (1) prevent irreversible evidence loss and enable adapter repair without
reacquisition; (2) remove bounded structured-extraction blind spots; (3) improve document
evidence selection and measure semantic research quality. Identity matching and scale
remain important, but neither repairs the observed loss. Increasing the row cap alone
does not establish a general recovery path. This ordering revises the former roadmap.

## Research and reuse

- [dlt production behavior](https://dlthub.com/docs/running-in-production/running)
  resumes pending normalization/loading before acquiring new data. Adopt the staged
  durability pattern, not its destination loader: Harvest additionally needs URL policy,
  leases, raw HTTP provenance, and conflicting immutable assertions.
- [Scrapy HTTP cache](https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#module-scrapy.downloadermiddlewares.httpcache)
  separates cache storage and policy. A response cache is useful, but is not an
  authoritative extraction ledger with job ownership and historical result revisions.
- [warcio](https://github.com/webrecorder/warcio) provides streaming WARC/ARC archive IO;
  its `capture_http` implementation wraps `http.client`. Do not substitute that capture
  path for the existing policy-enforced HTTPX transport. Revisit WARC interchange when
  external archival interoperability is a requirement; no new format is needed here.

Sources inspected on the decision date. The reuse decisions above are engineering
judgments, not claims that those projects cannot be extended.

## Contract

1. Commit the bounded, permitted response and a leased extraction task atomically.
2. Extract from the committed body. Commit assertions, their extraction membership,
   proposed follow-up tasks and task completion atomically in a second transaction.
3. A parser exception, observation-write rollback, or process death after checkpoint
   must not delete acquired evidence or require another GET.
4. Offline replay creates a new job and extraction revision referencing the original
   capture. It must not fabricate a retrieval timestamp or duplicate an HTTP capture.
   It uses installed deterministic adapters only, with no fetch/search/model tasks.
5. Historical revisions remain inspectable. The current view uses the latest successful
   extraction of the latest successfully interpreted retrieval per source. Failed/pending
   newer attempts must be visible, and must not silently clear previously known values.
6. Schema 1 upgrades transactionally with its evidence, observations, idempotency keys,
   schedules and active tasks preserved. Back up before upgrade; old binaries cannot
   open schema 2.

No malformed JSON auto-repair, browser/PDF adapter, record-cap increase, model-context
change, untrusted plugin sandbox, or universal completeness claim is part of this milestone.
