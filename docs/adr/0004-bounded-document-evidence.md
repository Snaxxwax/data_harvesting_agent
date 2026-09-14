# ADR 0004: Select and audit bounded document evidence

Date: 2026-09-14. Status: implemented in 0.4. Schema remains 3.

## Observed problem and milestone choice

Re-ran the four owned-source modes at `0d637c7`. Targeted malformed exports still retain
evidence; enumeration returns all 250 entities; failed continuous refresh retains an explicit
stale candidate. Deep Research acquires a report containing an explicit accreditation fact
after character 17,500, then sends only the first 14,000 characters to reasoning. Its missing
field is an input-selection failure, independent of model intelligence. Improving this
boundary benefits targeted, continuous and recursive research without another acquisition
backend or an entity-resolution hypothesis. It was stronger evidence than adding a new format.

## External alternatives inspected

- [SQLite FTS5](https://www.sqlite.org/fts5.html) supplies Unicode tokenization and BM25 ranking
  through the existing SQLite runtime. Its build-time availability must be handled explicitly.
- [Haystack DocumentSplitter](https://docs.haystack.deepset.ai/docs/documentsplitter) provides
  overlapping splits and source metadata. We reuse the overlap/provenance pattern; importing
  a pipeline framework for these bounded character windows is not justified here.
- [Sentence Transformers retrieve/rerank](https://sbert.net/examples/sentence_transformer/applications/retrieve_rerank/README.html)
  supports retrieval followed by cross-encoder ranking. It warrants a later semantic-recall
  evaluation, but adds models, deployment resources and a new evaluation problem.

## Implemented decision

Reuse SQLite FTS5 in an ephemeral per-attempt index. Search the existing adapter text with
literal, bounded terms from requested fields, the objective and recent proposed gaps. Missing
fields take priority; missingness now comes from all persisted assertions, not the 40-row
context sample. Field presence is still job-wide, not per-entity completeness.

Short text (at most 14,000 Unicode code points) stays intact. Longer text is split into
2,600-character windows with 400-character overlap. Select at most five: introduction,
conclusion, then round-robin ranked matches. Fill spare slots with deterministic interior
samples. Merge contiguous selected ranges and present them in original order. The complete
`SOURCE_TEXT`, including omission markers, remains below 14,000 characters. This is a fixed
implementation bound; ordinary token, call and cost budgets still apply to the entire prompt.

No additional model calls, service, dependency, persistent search index or schema migration
is introduced. SQLite builds without FTS5 use an explicitly recorded coverage-only fallback.
Other SQLite failures are not disguised as fallback success. Queries are quoted literal
tokens bound as SQL parameters, never user-supplied FTS expressions.

Before provider dispatch, `model_reserved` persists exact system/user messages, selection
version/backend/queries/spans, original adapter-text hash and length, selected/omitted counts,
normalizer version, capture/body identity, extraction revision, task attempt and reservation.
The body hash is checked before reasoning. Stored spans map model input back to original
adapter text in Unicode code points, with exclusive ends. Quotes must fit one contiguous
selected range; they cannot cite omission markers or bridge disconnected passages. Accepted
locators use the original offset. `model/2:<name>` versions the changed selection behavior.

Audit records survive a crash or invalid response. Reservations remain conservative and are
not refunded. A reclaimed task may call the provider again; this is not exactly-once billing.
Model results are still committed atomically with assertions and follow-up work. No model
result replay or response cache was added. Offline capture replay remains deterministic only.

HTML/plain-text adapters now warn when their existing 100,000-character normalization bound
omits a suffix. The acquired body remains intact. The selector's coverage numbers describe
adapter text, not full original documents. HTML normalization removes some nodes, and native
JSON/CSV reasoning still receives bounded structured adapter output.

## Evidence and revision of expectations

The original Deep Research case now exposes and extracts the accreditation quote, with
three scripted model calls and nine requests unchanged. The job remains partial because
its unsupported supplier format remains unsupported. A synthetic ten-document probe recovers
12/16 labeled quotes, compared with 2/16 for the old prefix and 2/16 for uniform sampling.
It retains both distant conflicting statements. These are input-coverage results, not model
truth, entailment, entity linkage, or general research-quality scores.

The four misses are material: unmatched vocabulary and three of six competing fields.
Therefore this selector is useful but not a complete document-reading strategy. Bounded
multi-pass coverage, unresolved-field scheduling and semantic retrieval should be compared
against these failures before increasing infrastructure. Full-document coverage would require
additional processing/cost accounting, durable pass state and an honest exhaustion policy.
Exact-quote acceptance does not prove that a model's field/value follows from its quote.

See [0.4 validation](../validation/v04-workloads.md) and the executable tests/benchmark.
