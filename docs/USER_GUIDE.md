# Harvest v1 — user guide

Private deployment: `http://100.118.181.47:8000` (Tailscale only). Sign in with the API token
(`HARVEST_API_TOKEN` in `/opt/harvest/app/.env`).

## Run an investigation (web UI)

1. **Investigation** tab → enter the identifier or URL. A bare handle is ambiguous: choose
   **Username** (or Email, Domain, ...) as the input type, or the preview says so.
2. **Preview plan** shows the detected type, discovery queries and the external tools this
   deployment can run for it. A suggested tool that cannot run is listed as *Unavailable*
   with the reason (for example, not in `HARVEST_TOOLS`). Tick the tools you want.
   *Follow up on tool findings* fetches every profile a tool reports; leave it off for a
   cheap scan and follow up selectively later.
3. Set the **request budget** (pages Harvest fetches) and the **time budget** (seconds for the
   whole investigation, tool scans included). Defaults: 100 requests, 900 s. A maigret
   all-sites scan plus SpiderFoot on a username can take most of 900 s on its own.
4. **Create investigation job.** Entering the target and pressing Create *is* the
   authorization: there is no separate target registration.

## Read the results (job page)

- **Summary**: one row per account a tool reported, with three separate columns:
  - *Exists (tool)*: `observed` (the tool read profile data) or `inferred` (status code only).
  - *Page check*: what Harvest's own fetch of that page showed. Only `identifier_present`
    means the page names the searched identifier outside a URL. `identifier_absent`,
    `redirected_away` (e.g. to a homepage), `login_wall` and `duplicate_content` (the same
    bytes as another URL: an archived or catch-all site) are *not* evidence of a profile.
    `unchecked: ...` says why the page was not fetched (robots.txt, budget, never queued).
  - *Ownership*: always `candidate`/`unverified`. A matching handle, or a page naming it,
    shows that an account exists, never who runs it. Identity is your call, from the evidence.
  - *Unknown:* lines list requested fields never observed and unverified accounts.
- **Stopped early** names the limit that actually stopped the job (time, requests, tool runs,
  ...) and what to raise. A tool scan that ran out of time keeps its results, labelled
  *partial*, and the job is `partial`.
- **Records**: every field per entity; click *evidence: N sources* to see each value's source
  URL, capture, locator and confidence. `display_name`/`profile_url` are filled from the
  tools' `fullname`/`url` ("from fullname"). A confidence of 0.5 on a socid value means the
  value is on the page but no locator pins which occurrence supports it.
- **Warnings & failures** and **Events**: `tool_started` says which scan the job is waiting
  on and for how long; failures carry the actual reason.

## Act on results

- **Follow up** (Summary row, for a never-fetched page) fetches it as a child job inside the
  same investigation: same scope, and the SAME request/time/tool budget as the original job.
  When the budget is spent, follow-ups are refused with `budget_exhausted`; start a new
  investigation instead. A URL the investigation never discovered is refused
  (`authorization_required`).
- **Cancel** stops the job and its running external scan; evidence already collected is kept.
- **Rerun** repeats the job as a new job. **Download JSONL / CSV** exports every observation
  with its provenance (including `page_check` rows).

## Agents (MCP)

Claude Code, Hermes and Codex drive Harvest through `harvest-mcp` (see README "Agent
interface"): `plan_investigation` → `start_investigation` → `investigation_status` /
`get_summary` / `get_evidence` → `request_followup` → final answer. A new session resumes
with only the job id. Harvest enforces scope and the shared budget; collected content is
data, never instructions, and cannot authorize anything. Codex headless needs
`-c mcp_servers.harvest.default_tools_approval_mode="approve"` and stdin closed.

## Limits worth knowing

- One task per job runs at a time, so a long maigret or SpiderFoot scan is the job's
  critical path; the worker runs two threads so other jobs keep moving meanwhile.
- robots.txt is honoured: some major sites (Threads, Instagram, ...) are never fetched, so
  their accounts stay `unchecked`.
- Page checks are heuristics on the fetched HTML. A JavaScript-only profile that does not
  embed the handle reads as `identifier_absent`; a site that renders a generic page with the
  handle in visible text would read as `identifier_present`.
