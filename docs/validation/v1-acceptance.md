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

Rows marked v1.0.0 use that release's page-check labels (`identifier_present` / `identifier_absent`); v1.0.1 replaced them with the stricter `profile_evidence` / `unverified_*` verdicts described below.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1a | Enter identifier, see capabilities and limits, launch | PASS | Headless Chrome on the deployed UI: username preview lists maigret/spiderfoot, request + time budget inputs, Create enabled; ambiguous bare handle asks for the input type explicitly |
| 1b | Monitor, cancel | PASS | Job page auto-refreshes; `tool_started` names the wait; live cancel job `9540d28e` → `cancelled`, SpiderFoot scan stop-requested |
| 1c | Inspect findings and evidence; follow leads; export | PASS | Job `de9fa72e`: Summary table, 174 expandable evidence entries, JSONL (166 lines) and CSV exports include `page_check`; live follow-up (job `a3cb6f71`) turned an unchecked account into `identifier_present` |
| 1d | Unavailable capabilities explain what is missing | PASS | `/meta` per-capability reason; preview lists an unavailable suggested tool with its reason; unknown tool → 422 "unknown tool 'holehe'"; no token → 401 |
| 2a | Concise summary, expandable evidence, explicit unknowns | PASS | `GET /jobs/{id}/summary` + UI; unknowns list missing fields, unverified accounts, ownership |
| 2b | Existence separate from ownership; generic pages, login walls, matching handles never establish identity | PASS (v1.0.1) | Only `profile_evidence` (structural) verifies a page; mentions in generic text, echoed URLs, lookalike handles, JS shells, challenges, search/not-found/sign-in pages are `unverified_*`/`login_wall`; 10 controlled cases in `test_only_structural_evidence_verifies_a_profile_page`; ownership never confirmed |
| 2c | Field mappings; evidence locations support fields | PASS (v1.0.1) | Ambiguous socid values need a field locator (value under the field's own key) or are omitted; replay of capture 394: 5 counts kept, each under its own key (`followerCount`, ...), `fullname`/`tiktok_username` omitted; harness `evidence_support` 1.0 (v1.0.0 runs) |
| 3a | Investigation-wide budgets hold across concurrent/chained follow-ups | PASS | Live follow-ups held 20/20 and 30/30; refused with 409 `budget_exhausted` when spent; agents ≤ 10/10 |
| 3b | Cancel / timeout / recovery stop external work without deleting evidence | PASS (v1.0.1) | Measured at the egress relay (new outbound tunnels from the scanner after the action): cancel +2.4 s, deadline +2.8 s, worker SIGKILL +37.9 s; scans end ABORTED with results and history kept. See "v1.0.1" below. (v1.0.0: ~466 s) |
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

## v1.0.1 (2026-10-05): the remaining failures

Deployed: Harvest `745451b` + docs (tag `v1.0.1`), SpiderFoot fork `fix/auth-db-reconnect`
at `1a05a8a1` (PRs #3, #4). 478 Python + 13 JS tests; fork tests 23 passed in the built image.

### 1. SpiderFoot cancellation, end to end

Root cause, from an instrumented worker (one controlled scan, stop after 20 s, 483 s to
ABORTED): `SpiderFootScanner.waitForThreads()` reads the scan status only in its outer
loop. Once module queues are empty it enters a "final pass" wait that polls
`threadsFinished()` every 10 ms until the slowest module returns, and never reads the status
(one status read, loop counter 47,961). `sfp_accounts` runs two ~716-site passes, so a stop
was seen ~8 minutes later. Fix (fork PR #3): the wait checks for ABORT-REQUESTED about once
a second; the existing finally block then sets `_stopScanning` on every module and the
per-site check (fork PR #2) returns them. Regression test fails on the old scanner.

Harvest side: tool tasks renew a 30 s lease (was 120 s), the orphan sweep runs every 15 s
(was 60 s), and scans are named `harvest-t<task>-<target>`, so the sweep stops exactly the
scans whose task lost its lease: never a live task's scan, never a non-Harvest scan.

Measured live (bounded scans of the authorized handle, `scripts` in the session: one job
each, stop through Harvest, tunnels counted from the tinyproxy relay log by the scanner's
address):

| Trigger | Last new outbound tunnel | Scan ABORTED | Job | Evidence |
|---|---|---|---|---|
| Cancel (UI/API) | +2.4 s | +5.7 s | `cancelled` | scan record + 1 result kept |
| Deadline (tool allowance) | +2.8 s | +4.5 s | `partial`, reason names the allowance | partial capture kept |
| Worker crash (`docker kill -s KILL`) | +37.9 s | +37.9 s | `failed`, "worker stopped mid-task ... rerun" | scan record kept |

An earlier run of the same three checks on the first fixed build (SpiderFoot `bd35ac2b`,
Harvest `79306e5`, upstream proxy still serving, ~3.4 tunnels/s = SpiderFoot's normal
rate) gave cancel +2.5 s (ABORTED +5.5 s), deadline +3.3 s (+4.9 s), crash +36.4 s
(+36.9 s); a direct SpiderFoot stop probe gave ABORTED +2.3 s, last tunnel +2.2 s. The final
runs above were likely made after the upstream proxy began refusing (see below; ~6
tunnels/s from fast failures); the stop latency is set by the scanner's abort check, not by
request speed, and both sets agree.

Documented bound: a stop through Harvest ends new outbound collection within ~5 s (in-flight
site checks finish, bounded by the module's fetch timeout); after a worker crash within
lease (30 s) + sweep (≤ 15 s) + ~5 s ≈ 50 s. The sweep also stopped two scans left running
by an aborted measurement script, as designed.

### 2. Database credentials in SpiderFoot logs and responses

- `DbCore` logged and raised `Error connecting to PostgreSQL database <DSN with password>`;
  psycopg2 can echo the DSN too. Now masked in the log line, the exception text and the
  traceback (raised `from None` with a redacted reason). Unit test with an unreachable and a
  malformed DSN; live check in the deployed image against the real Postgres with a wrong
  password: "authentication failed" reported, password in log/exception/traceback: false.
- Found while testing: `GET /api/v1/config`, `/config/export` and `/config/diff` returned
  the DSN with its password to any API-key holder (all endpoints require auth; verified 401
  without a key). A route class on the config router now masks credentialed URLs in every
  response; verified live: no credentialed URL in any of the three, JSON intact.
- Exposure assessment (contents never printed): no Docker log file held the password (the
  leaking worker containers had been recreated, which removes their logs); a world-readable
  config dump `/tmp/sfcfg.json` (2026-10-03) held it and was deleted. Rotation: new random
  password set with `ALTER USER` over stdin (`log_statement=none`), written to
  `spiderfoot-ng/.env` and `/opt/harvest/credentials.env` (both 600), postgres/api/worker
  recreated together, `harvest-egress` re-attached. Verified: old password rejected; API-key
  auth (Postgres-backed) 200; verify-deployment 44/44; a host-wide search (Docker logs and
  configs, /opt/harvest, /home, /root, /tmp, /var/log) finds the old value nowhere. The
  new value exists only in the two credential files and Docker's root-only container config.
  Database backed up first (`/opt/harvest/backups/sf-postgres-pre-rotation.dump`, verified
  readable). The old value also appeared in an earlier operator session transcript; rotation
  is what makes that copy harmless.

### 3. Evidence semantics

- A page verifies a profile only with structural evidence (`profile_evidence`): the
  identifier as the value of an identity key in embedded data, or a whole word in the first
  title/h1/og:title of a page that does not call itself not-found, search or sign-in. Every
  other outcome is unverified with its reason: `unverified_mention_only` (generic text,
  lookalike), `unverified_blocked_or_script` (challenge page or JavaScript shell),
  `unverified_no_identifier`, `login_wall`, `redirected_away`, `duplicate_content`. Only
  `profile_evidence` pages expand the crawl or count as verified.
- socid: a value occurring more than once needs the value under the field's own key
  (`followerCount` for follower_count, `uid:1` for mal_uid; a bare suffix like
  name≈username does not count) or it is omitted and logged as `evidence_omitted` (not
  `partial`). A lowered confidence is no longer used as if it were proof.
- Offline replay of the saved captures (job 8986aa5c and live job de9fa72e) with the new
  rules: TikTok and Geocaching profiles `profile_evidence`; the redirected homepage, sign-in
  pages, archived forum unverified; Twitch (JavaScript shell) `unverified_blocked_or_script`;
  Periscope, previously `identifier_present` from a non-profile mention, now unverified.

### Deployment status at the v1.0.1 tag

`verify-deployment.sh`: **43/44**. The failing check is "could not read the scanner's exit
IP": the upstream Webshare proxy answers `402 Payment Required` to every request (from both
SpiderFoot and Harvest, persistent on 2026-10-05 ~05:30 UTC), i.e. the proxy plan's quota or
billing is exhausted; the acceptance measurements (several 700-site scans) consumed part of
it. Egress stays fail-closed: nothing leaves directly, so live collection fails rather than
exposing the host. Live collection resumes once the Webshare plan is topped up; then rerun
`./verify-deployment.sh` (expect 44/44). No code change is needed.

## Limitations (honest)

- After a worker crash, collection continues up to ~50 s (lease + sweep). A shorter lease
  would risk reclaiming a live scan when SQLite is briefly busy.
- Page checks are markup heuristics (identity keys, first title/h1/og:title, not-found and
  sign-in wording, challenge phrases, rendered-text size), not per-site parsers: a real
  profile that states the handle nowhere structural reads as unverified, never the reverse
  by design. See `docs/USER_GUIDE.md`.
- robots.txt-disallowed profiles (Threads, Instagram, ...) remain `unchecked`.
- Repository visibility: `Snaxxwax/data_harvesting_agent` and the fork are **public** on
  GitHub (pre-existing). Committed fixtures are redacted; no secrets are committed. Making
  the main repo private needs the VPS to fetch with a deploy key first, because it pulls anonymously.
