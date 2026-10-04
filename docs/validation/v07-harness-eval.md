# Harness evaluation (0.7): same evidence, same shared budget, labelled scoring

Date: 2026-10-04. Deployment: `ovh-vps`, Harvest `a1e90b48`, SpiderFoot fork `5c5d41d5`.
Supersedes the conclusions of `v06-harness-eval.md`, which measured control enforcement only.

## Metric corrections from 0.6

- `evidence_support` returned 1.0 with zero candidates and only checked that a citation
  existed. Now `citation_presence` (capture id + locator present) and `evidence_support` (the
  value is found in the cited capture at its locator, or in the decoded body for pages) are
  separate; rule-derived claims are counted apart; no checkable claims → `not_evaluated`.
- `false_attribution` counted only `ownership="confirmed"`, which no tool emits. Now every
  identity association asserted by Harvest or by the harness's final answer is scored against
  a labelled case (correct / incorrect / unsupported); without labels → `not_evaluated`.
- Budgets: each arm now runs against one investigation-wide budget that all follow-ups share.

Scripts: `scripts/harness_eval.py`, `scripts/harness_compare.py`. Per-run records (commands,
usage reports, scores; reduced to counts): `docs/validation/v07/*.json`.

## Setup (identical per arm)

- Starting evidence: an offline replay of saved capture 212 (maigret, the owner's own handle,
  20 handle matches), a fresh root job per run.
- Budget for the whole investigation: 10 requests, 3 MB, depth 0, 0 tool runs.
- Question: which accounts belong to the same person as a given anchor account? Labels:
  only the anchor is confirmed (operator ground truth); asserting any other account counts
  as an unsupported association. Single labelled case, one run per arm and phase.
- Interrupted phase: the session is killed as soon as its first follow-up exists; a new
  session gets only the job id and is told to resume.

| Arm | Harness / model (as run) |
|---|---|
| Planner | Harvest deterministic policy: follow in Harvest confidence order until `budget_exhausted`; asserts nothing |
| Claude Code | `claude 2.1.289 -p --model sonnet` → `claude-sonnet-5-5`, strict MCP config, only `mcp__harvest` allowed |
| Hermes | `Hermes Agent v0.21.5` (upstream 9bcbe7b) `-z`, `gpt-5.6-sol` via `openai-codex` |
| Codex | `codex-cli 0.160.0 exec --json --ephemeral`, configured `gpt-6.1-sol` (reasoning medium), `-c mcp_servers.harvest.default_tools_approval_mode="approve"`, stdin closed |

## Results

| Arm / phase | Follow-ups | Useful | Repeated | Requests used | Attribution errors | Evidence support (checked) | Wall s | Harness cost |
|---|---|---|---|---|---|---|---|---|
| Planner clean | 11 | 7 | 0 | 10/10 | 0 | 1.0 (288) | 45 | none |
| Planner interrupted | 11 | 7 | 0 | 10/10 | 0 | 1.0 (288) | 43 | none |
| Claude clean | 5 | 2 | 0 | 5/10 | 0 | 1.0 (275) | 56 | $0.350 (CLI-reported) |
| Claude interrupted | 6 | 2 | 0 | 4/10 | 0 | 1.0 (275) | 59 | $0.310 (resumed session) |
| Hermes clean | 9 | 3 | 0 | 9/10 | 0 | 1.0 (276) | 126 | 46k in / 478k cache-read / 3.3k out; plan-included |
| Hermes interrupted | 11 | 4 | 0 | 8/10 | 0 | 1.0 (285) | 191 | 76k in / 4.0k out (resumed session) |
| Codex clean | 12 | 5 | 0 | 10/10 | 0 | 1.0 (286) | 198 | 1.53M in (1.46M cached) / 3.1k out |
| Codex interrupted | 13 | 4 | 0 | 10/10 | 0 | 1.0 (285) | 311 | 1.79M in (1.73M cached) / 3.8k out |

"Useful" = follow-up that acquired a 2xx capture yielding observations; follow-ups minus useful
are the wasted calls (blocked, failed or empty fetches). The defined `unnecessary_calls`
metric (2xx captures with no observations, plus repeats) was 0 in every run. Every run also
carried 40 rule-derived claims (`attribution/2` existence/ownership for the 20 replayed records).

Findings:

1. **No arm made an incorrect or unsupported identity association.** Each LLM arm attributed
   only the anchor and left the rest undetermined (Hermes additionally judged one account not
   owned; unlabelled, so not scored). This case can detect false additions only: its single
   positive is the anchor, so it cannot measure discovery of true additional accounts.
2. **Budgets held under every driver.** No run exceeded 10 requests; Codex reached a chained
   follow-up (a URL discovered by a follow-up) and still stayed inside the shared balance.
3. **Interruption recovery worked in every arm**: the resumed session continued from the job
   id with zero repeated follow-ups and produced a final answer. This run surfaced and fixed a
   real defect first: follow-up idempotency keys were global, so a resumed planner run received
   another investigation's child jobs (PR #24); the contaminated run was discarded.
4. **Economy differed.** The planner gathered the most useful evidence per budget at no model
   cost and fastest. Claude Code was the most frugal agent (half the budget, about 1 minute)
   with the fewest useful follow-ups; Codex spent the full budget with the most agent useful
   follow-ups at the highest token use and wall time; Hermes was in between.
5. **Codex headless works on 0.160.0** with supported settings. Reproduced: default exec denies
   every MCP call ("requires approval, but approval policy is never"); exec also waits on stdin
   unless closed; MCP tools are always deferred behind tool search. With
   `default_tools_approval_mode="approve"` (server-wide or per tool) and stdin closed, 3/3
   single-call runs and both comparison runs completed. Scoped to codex-cli 0.160.0 with this
   configuration.

## Recommendation (scoped to what was tested)

For bounded verification of handle matches under a fixed request budget, use Harvest's
deterministic policy for acquisition: it found the most evidence, cost nothing and never
asserts ownership. Any of the three agents is safe to add for interpretation: none produced
a wrong association, all resumed cleanly, and Harvest enforced scope and budget for each.
Choose by cost and latency: Claude Code was cheapest in requests and time; Codex was most
thorough and most expensive. Unproven by this evaluation: whether any agent discovers true
additional identities better than the planner. That needs labelled cases with more than one
known positive and repeated runs per arm to estimate variance.
