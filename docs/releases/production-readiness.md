# Private OVH readiness release

Status: **candidate; not deployed or approved for production**. The release keeps SQLite,
Compose, Tailscale-only ingress and the existing proxy/providers. Do not treat this document,
an HTTP 200, or the preliminary soak as production sign-off.

The [verification ledger](verification.json) records the source hash, retained test logs and
outstanding gates. Latest local checks: 29 focused Python tests, 57 tool regression tests and
32 JavaScript tests pass (the Python selections overlap); lint/format checks and a separate
schema-1-to-4 preservation drill pass. Both pinned native hook source guards pass against local
tool distributions. Full production-image verification remains outstanding.

## Candidate behavior

- New planner/UI investigations default to `deep`, displayed as Thorough: 60 minutes from
  admission across root and children, 300 Harvest requests, eight tool runs, three distinct
  one-hop identifiers. Explicit JobSpec callers keep their existing defaults.
- Admission is limited to two investigations and two executing tool tasks. Thorough uses
  all enabled, eligible Maigret sites at both levels; Standard uses 500. Tool ceilings are
  GHunt 120, SpiderFoot 900 and Maigret 1,800 seconds, bounded by remaining investigation time.
- Schema 4 adds executions, content-addressed native artifacts, check outcomes and incidents.
  Original job specifications, observations and historical terminal statuses remain intact.
  Legacy coverage is unknown. New incomplete acquisition reports partial.
- Native checkpoints survive task cancellation/worker loss. No interrupted external tool
  task is automatically repeated. Restored active jobs/tasks become interrupted/partial and
  cancelled respectively. Recovery must reconcile external scans before admitting new work.
- `/health` is liveness. Authenticated `/ready`, `/incidents`, `/jobs/{id}/coverage` and
  `/jobs/{id}/artifacts/{artifact_id}` expose operational and acquisition evidence. Artifact
  bodies are hash-checked; browser and agent interfaces expose the same records.
- Check outcomes distinguish found, absent, blocked, errored, unsupported, skipped and
  unfinished. Execution completion is separate from coverage. An empty or failed check does
  not establish that an account is absent. Matching profiles retain candidate attribution.
- Search captures retain provider failure reasons and accepted/rejected leads. Readiness
  samples every minute; one known-answer query runs every 30 minutes through SearXNG's
  configured proxy route. SearXNG owns cooldowns. Provider degradation stays visible even
  when another provider returns results.
- Disk below 20% free opens an incident; below 10% blocks new admission. Evidence is retained.
  Tool traffic is outside Harvest's request and dollar counters; these do not cap proxy spend.

## Evidence and remaining gates

| Gate | Current evidence | Remaining requirement |
| --- | --- | --- |
| Deterministic parity | Sanitized Maigret/GHunt/SpiderFoot fixtures; audited SpiderFoot sets both normalize to four usable findings | Final candidate test report; retain every exclusion reason and nested field |
| Workflow | Offline email → discovered handle → Maigret, all-site follow-up and shared-budget tests pass | Verify final image uses declared root/follow-up breadth |
| Failure injection | Native found/absent/blocked/errored/skipped/unfinished tests; malformed/truncated checkpoint and expired-lease preservation tests | Final-image proxy/CAPTCHA/expired-credential/deadline/cancellation/worker-death/database-restart matrix |
| Recovery | Transactional migration, no automatic external rerun, paginated namespace-aware orphan sweep; declarative networks and credential volume | Recreate containers and prove credentials/networks survive; measure orphan stop within 60 seconds after dependencies recover |
| Matched live tools | `scripts/live_parity.py` records a durable ceiling of twelve invocations and field-level discrepancies | Two matched rounds per tool using existing authorized targets; review every discrepancy, without silently repeating interrupted invocations |
| Search | [Canary evidence](search-canary.json): DuckDuckGo returned relevant IANA sources; Bing returned irrelevant sources; Brave rate-limited | Recheck final deployment; retain degradation. Minimum search gate passed on the earlier deployment only |
| Endurance | Preliminary offline replay soak active at concurrency two, over six hours without a recorded failure | Full 24 hours on frozen candidate content, with source hash, memory/storage bounds and API latency report; preliminary run predates subsequent edits |
| Restore | [Restore drill](restore-drill.json): 40.39 seconds; 513 evidence blobs, 359 SpiderFoot events, 14 configuration files; both databases restored in an isolated internal network; migrated schema 4; no active jobs | Repeat with final candidate; active-job recovery fixture and paired database/image rollback demonstration |
| Existing checks | Earlier full Python run: 518 passed, four failures; corrected assertions/partial reasons pass focused reruns. JS: 32 tests pass | Entire Python/lint/migration/deployment suite against final production image with tools; latest sandbox denies sockets and local Docker |

The current environment blocks new SSH sockets, Docker access and loopback test servers.
An attempted full rerun stops in the HTTP fixture with `PermissionError: Operation not
permitted`; the artifact TestClient check also hangs in its thread portal. These are not
successful verification results. No release gate is waived because of the environment.
The checked-in source manifest intentionally says `release_allowed: false`.

## Build and validation

1. Freeze source and run `python scripts/release_manifest.py`. Record its content-qualified
   revision in `HARVEST_BUILD_REVISION`. Recompute after any source/patch/configuration edit.
2. Build Harvest with `HARVEST_TOOL_PACKAGES="maigret==0.6.6 ghunt==2.3.4"`. Dependencies for
   each isolated tool are constrained under `deploy/tool-hooks/`. Source/version guards must
   pass. `/app/release-manifest.json` records dependencies, patch hashes, database hash and
   eligible-site count. Disabled Maigret sites are excluded from that count.
3. Apply `deploy/spiderfoot/production-readiness.patch` to the audited SpiderFoot fork
   (`c987c298`, also contained in deployed merge `f3b4ec3c`), first using `git apply --check`.
   Build both API and scanner from the same patched revision. Record patch SHA-256 and image
   identities. The patch changes log flushing, per-site account checks, TLS verification,
   disabled optional integrations and the no-repeat guard for interrupted scans.
4. Run Python, JavaScript and lint checks, including the portable single-log regression.
   Use the production runtime with tools installed for final verification; a tools-empty
   developer image does not satisfy gate 9. Retain logs and image identities with the report.
5. Restore the latest verified archive into an isolated project. The existing drill is:

   ```sh
   .venv/bin/python scripts/restore_drill.py \
     --archive /home/pop/backups/harvest/harvest-20261007T105110Z.tar.gpg \
     --keyring /home/pop/.local/share/harvest-backup-key \
     --image "$HARVEST_IMAGE" --report docs/releases/restore-drill-final.json
   ```

   Run a fresh 24-hour `scripts/readiness_soak.py --directory <private-new-directory>` against
   frozen content. A changed source hash invalidates the run. Keep API responsiveness,
   maximum RSS, database growth, failures and elapsed duration in the report.
6. Run the bounded live runner **inside the release environment**, with a private JSON file
   mapping `maigret`, `ghunt`, `spiderfoot` to the already authorized audit targets. Pass
   `--targets`, a fresh `--directory`, and the actual `--spiderfoot-revision`. It refuses an
   existing ledger. Standalone and Harvest share settings, credentials, databases, proxy,
   retries, connection limit and timeout. A distinct scan namespace prevents production
   recovery from stopping audit scans. Preserve the directory and its SQLite/native reports.
   Inspect all field discrepancies; remote sites may change between invocations. Do not
   restart the runner in a new directory to evade the twelve-invocation ceiling.

## Deployment and rollback

After every gate passes, drain investigations, stop admission, and wait for active tasks and
their external scans to finish or explicitly cancel them. Create and verify a fresh paired
pre-release backup. Record previous image IDs, the archive hash, configuration and tool builds.
Do not stop the database before the online SQLite backup/PostgreSQL dump completes.

Use `scripts/pin_release.py` on the deployment host to resolve tested builds and unchanged
sidecars into `HARVEST_IMAGE`, `SF_API_IMAGE`, `SF_SCANNER_IMAGE`, `SF_FRONTEND_IMAGE` and
`FLARESOLVERR_IMAGE`; save them in protected `.env.release`. The script refuses to overwrite
previous rollback identities. Use the same Harvest image for API,
worker and monitor. Copy the readiness overlay and `spiderfoot-profiles.json` beside existing
production overlays. Load Compose in this order: base, OVH override, existing search/proxy
overlays, GHunt overlay, readiness overlay. Review the rendered configuration without printing
secrets. The SpiderFoot overlay requires explicit API/scanner release images too. Use
`--no-build --pull never` for rollout so Compose cannot rebuild a tested identity implicitly.

Create `harvest-egress` through the Harvest proxy project before recreating SpiderFoot. The
scanner must join only its internal backend/DNS networks and `harvest-egress`; its API also
has the declared Harvest attachment. Do not restore manual `docker network connect` steps.
Confirm all published ports remain Tailscale-bound. MinIO, Qdrant and Vector flags stay false.

Start the matching API/worker/monitor and tested SpiderFoot builds. Verify authenticated
readiness, credentials, effective profiles, connectivity, TLS failures, direct-egress denial,
incident transitions/recovery and the bounded live acceptance checks. Reconcile orphan scans
before admitting investigations. Record actual recovery timings. Never launch investigations
from an ordinary health check.

Rollback pairs the **previous images and matching pre-release databases/configuration**.
Stop API, workers and monitor before swapping database volumes; stop/reconcile external scans.
Restore into fresh volumes, keep current volumes intact for evidence, and apply the interrupted
job rule before starting any worker. Do not run old code against schema-4 data. Restore the
matching PostgreSQL dump and protected configuration; verify blob hashes, database integrity,
credentials, networks and API access. Keep acquisition stopped until checks pass. The isolated
rollback demonstration is still a gate, not an already tested procedure.

## Backups and alerts

The OVH daily backup service and workstation hourly pull timer were installed independently
of the application release. Source units are under `deploy/ovh-vps/systemd/`. Server backups
use SQLite's online API, a PostgreSQL custom dump, protected configuration and GHunt credentials,
plus checkpoint journals when present. The manifest includes file hashes and image identities.
The complete archive is encrypted to the dedicated recipient before leaving OVH.

Encrypted copies live in `/home/pop/backups/harvest`; retention keeps the union of seven daily,
four weekly and three monthly copies. The decryption key is filesystem-protected separately at
`/home/pop/.local/share/harvest-backup-key`, outside ordinary app configuration. Protect an
independent recovery copy of that key. Plaintext verification/restore scratch is temporary and
private. Pull verification checks decryption, manifest hashes, SQLite integrity and every blob
hash before acknowledging freshness on OVH. Both source creation and verification must be
within 24 hours; a repeated pull cannot make an old backup fresh.

Update the installed server backup script when rolling out the new readiness/profile/release
configuration so those files are included too. Install/enable `harvest-incidents.timer` after
the new `/incidents` endpoint is deployed. The notification bridge defaults to SSH alias `ovh`
over Tailscale, obtains the API token only inside the existing API container, and persists
notification state locally. It sends one desktop notification per incident transition and
recovery while this workstation is online. The incident itself remains on OVH.

Workstation outages prevent a guaranteed 24-hour off-host recovery point. No automatic evidence
deletion repairs disk/backup incidents. Verify the latest receipt and investigate stale copies.

## Explicit capability limits

GHunt 2.3.4 cannot extract names or provide Maps photo/review detail; those checks are
unsupported. Native profile, Play Games, Maps statistics and Calendar data remain available
where the account/services provide them. Empty profile shells are excluded explicitly.

The initial seven-module SpiderFoot email profile is unchanged. `sfp_accounts` has structured
site outcomes; modules without that contract disclose unsupported per-check coverage rather
than inferring success from missing errors. Full module/site instrumentation and the patched
scanner's live log path still require release validation. TLS-incompatible sites fail checks.
The preliminary restore did not contain active jobs, so it does not prove active-job restore
semantics. The final image and patched scanner have not yet passed all deployment gates.
