# Harvest v1.0.0 — acceptance checklist

Date: 2026-10-05. Deployment `ovh-vps` (private, Tailscale only). Harvest: see "Deployed
revisions" below. SpiderFoot fork `Snaxxwax/spiderfoot` `fix/auth-db-reconnect` at
`75ca484c` (merge of PR #2, code `1835ad96`), plus the tracked compose overlay
`deploy/ovh-vps/spiderfoot-core.yml`.

Live checks used only the previously authorized test identifier (the operator's own handle).
Everything else is offline: 465 Python tests (27 in `tests/test_v1_crawl_quality.py`, built
from redacted copies of captures 393–401) and 13 JS tests. `verify-deployment.sh`: 44/44.

## Root causes for job 8986aa5c (captures 393–401)

Producing revision: image built from `a1e90b48` (16:27 UTC); containers restarted at
`87e7a9a` (16:54, docs-only change); job ran 20:15–20:31. Not a regression; each defect was
present since the feature that carries it.

| Observed | Root cause | Fix (PR) |
|---|---|---|
| Ten-minute inactivity gap; spiderfoot task "ValueError: invalid source or adapter result" | SpiderFoot's celery child failed the scan in 0.1 s ("connection pool exhausted": an 81-minute scan earlier in the same child had leaked its Postgres pool). The scan never left its first state (`started` = 0), so Harvest polled it for its whole 600 s; the timeout's ValueError was then persisted generically | `SF_CELERY_MAX_TASKS_PER_CHILD=1`; a never-started scan fails in 180 s with an actionable message; Harvest-written errors persist verbatim; `tool_started` event (#26) |
| Ran past the 900 s deadline (20:31:38) | Tool runs used `HARVEST_TOOL_TIMEOUT` regardless of the job's remaining wall clock | Allowance = min(timeout, remaining − 15 s); a stopped SpiderFoot scan keeps its events as partial (#26, #28, #29) |
| Wall-clock stop at 26/100 requests, UI said "raise the request budget" | UI text hard-coded for every `budget_exhausted` | `stop_advice` names the limit that fired (#26) |
| `display_name`/`profile_url` missing despite `fullname`/`url` | Maigret's field names never mapped to the requested names | `FIELD_ALIASES`, report-only (#26) |
| Auth pages crawled; generic archived-site pages returned | Links followed regardless of target (log in → OAuth → Google sign-in); a profile URL redirected to a homepage (50 links queued); an archived forum served one identical page for `/u/<h>` and `/u/<h>.json` | Sign-in/OAuth/sign-up links not followed; per-page `page_check`; only `identifier_present` pages expand (#26) |
| `ambiguous-value` socid locators at confidence 1.0 | Default claim confidence | 0.5 when no locator pins the occurrence (#26) |
| Second generic failure (Sign in with Apple) | Invalid redirect `Location` raised a bare ValueError | Policy block "redirect to a non-HTTP(S) or invalid URL" (#26) |

## Checklist

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1a | Enter identifier, see capabilities and limits, launch | PASS | Headless Chrome on the deployed UI: username preview lists maigret/spiderfoot, request + time budget inputs, Create enabled; ambiguous bare handle asks for the input type explicitly |
| 1b | Monitor, cancel | PASS | Job page auto-refreshes; `tool_started` names the wait; live cancel job `9540d28e` → `cancelled`, SpiderFoot scan stop-requested |
| 1c | Inspect findings and evidence; follow leads; export | PASS | Job `de9fa72e`: Summary table, 174 expandable evidence entries, JSONL (166 lines) and CSV exports include `page_check`; live follow-up (job `a3cb6f71`) turned an unchecked account into `identifier_present` |
| 1d | Unavailable capabilities explain what is missing | PASS | `/meta` per-capability reason; preview lists an unavailable suggested tool with its reason; unknown tool → 422 "unknown tool 'holehe'"; no token → 401 |
| 2a | Concise summary, expandable evidence, explicit unknowns | PASS | `GET /jobs/{id}/summary` + UI; unknowns list missing fields, unverified accounts, ownership |
| 2b | Existence separate from ownership; generic pages, login walls, matching handles never establish identity | PASS | Columns Exists / Page check / Ownership; ownership never confirmed; `test_page_checks_separate_real_profiles_from_generic_pages`; agents asserted only the labelled anchor |
| 2c | Field mappings; evidence locations support fields | PASS | Aliases test; `identifier_present` locator verified against the capture bytes; harness `evidence_support` 1.0 over 276–285 checked observations per agent run |
| 3a | Investigation-wide budgets hold across concurrent/chained follow-ups | PASS | Live follow-ups held 20/20 and 30/30; refused with 409 `budget_exhausted` when spent; agents ≤ 10/10 |
| 3b | Cancel / timeout / recovery stop external work without deleting evidence | PARTIAL | Harvest stops promptly and keeps evidence; timeout keeps partial events (job `12771d5b` → `partial`); worker restart → orphaned scan ABORTED at +133 s (job `0e4ba970`/rerun). **SpiderFoot itself honours a stop late**: a cancelled username scan kept running its `sfp_accounts` passes for ~466 s before ABORTED (see Limitations) |
| 3c | Errors actionable and secret-free; partial results explain the stop | PASS | Failure reasons persisted verbatim from Harvest's own messages; job reason names the first failure; `partial` with "time allowance" advice |
| 3d | Crawl avoids auth/navigation loops and duplicate expansion | PASS | Live job `de9fa72e`: 0 auth-like task keys; `redirected_away`/`identifier_absent`/`duplicate_content` pages expand nothing |
| 4 | Agents conduct and resume by job id; Harvest enforces scope/budget | PASS | `docs/validation/v1/*-interrupted.json` (below); undiscovered URL → 403 `authorization_required` |

### Agent resume runs (harness `interrupted` phase: session killed after its first follow-up, new session gets only the job id)

| Agent (as configured) | Follow-ups | Useful | Repeated | Requests | Attribution errors | Evidence support | Wall s |
|---|---|---|---|---|---|---|---|
| Claude Code `claude -p --model sonnet`, strict MCP config | 8 | 3 | 0 | 9/10 | 0 | 1.0 | 105 |
| Hermes `hermes -z` (openai-codex provider) | 8 | 3 | 0 | 9/10 | 0 | 1.0 | 214 |
| Codex `codex exec --json --ephemeral -c mcp_servers.harvest.default_tools_approval_mode="approve"` | 10 | 4 | 0 | 10/10 | 0 | 1.0 | 277 |

Every agent asserted only the labelled anchor as owned; none made an incorrect or unsupported
association. One run per agent, single labelled case: this shows the configurations work
and stay bounded; it does not measure discovery quality or variance.

## Limitations (honest)

- **SpiderFoot stop latency.** After Harvest's stop request, SpiderFoot reported
  `ABORT-REQUESTED` but completed both `sfp_accounts` passes (716 sites each) before
  `ABORTED`, ~8 min, through the egress proxy. The fork patch that checks for a stop
  between sites is correct but did not shorten this live: the running module instance never
  sees `_stopScanning`. Next step: log in `SpiderFootScanner.waitForThreads` when
  ABORT-REQUESTED is seen and compare the module object it flags with the one executing
  `handleEvent` (modern-module wrapper suspected).
- SpiderFoot's celery log prints its Postgres DSN including the password on connection
  errors (upstream `db_core` message). The logs are on the private host only; redacting it
  is a fork patch not yet made.
- Page checks are HTML heuristics (identifier outside URL context; redirect/path; sign-in
  path names). See `docs/USER_GUIDE.md`.
- robots.txt-disallowed profiles (Threads, Instagram, ...) remain `unchecked`.
- Repository visibility: `Snaxxwax/data_harvesting_agent` and the fork are **public** on
  GitHub (pre-existing). Committed fixtures are redacted; no secrets are committed. Making
  the main repo private needs the VPS to fetch with a deploy key first, because it pulls anonymously.
