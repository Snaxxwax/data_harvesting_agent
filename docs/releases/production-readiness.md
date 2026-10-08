# Private OVH readiness release

Status: **approved and deployed 2026-10-08** (admission reopened after acceptance). The release
keeps SQLite, Compose, Tailscale-only ingress and the existing proxy/providers.

| Identity | Value |
| --- | --- |
| Source | `8f62ce6f6830-a8f85a3b04b0` (branch `release/v1.0.1-readiness`, commit `8f62ce6`) |
| Harvest api/worker/monitor | `sha256:7b0508061d0673668188d3c4904978271e11bd94fe159fac2614b4a6edbda8fb` |
| SpiderFoot API | `sha256:defe0cfda9a227e46a9eba836b193cbbd6b35e3ead331d1d345996b8a8be0cbf` |
| SpiderFoot scanner | `sha256:709c056f03c82ca7282fb04d7fdb29b5a42271fb1fc761035b6b68591c74f531` |
| SpiderFoot source | fork `f3b4ec3c` + `deploy/spiderfoot/production-readiness.patch` (`c0218903…`) |
| Tools | Maigret 0.6.6 (5,178 eligible sites, DB `ef6ca66e…`), GHunt 2.3.4 |
| Pre-release backup | `harvest-20261008T200626Z.tar.gpg`, sha256 `6a0d4648…`, verified off-host |
| Rollback identities | Harvest `8b1269c0…`, SF API `9c9b30cc…`, scanner `167ee0ad…` ([previous-identities.txt](previous-identities.txt)) |

The [verification ledger](verification.json) is the authoritative record of every gate below.

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

## Evidence

| Gate | Result |
| --- | --- |
| Tests | 536 local Python, 523 in the production image with tools (git-dependent patch test run with ephemeral git), 32 JS, ruff, format, diff check ([logs](logs/)) |
| Migration | Copy of production schema 3 → 4 in the candidate image: 223 jobs, 146 idempotency keys, immutable hashes preserved ([report](migration-check-production.json)); production migrated on rollout, integrity ok |
| Failure injection | Final image: cancellation, worker death (Maigret, SpiderFoot), deadline, SpiderFoot DB + API restart, proxy failure, invalid GHunt credential, container recreate, active-job restore — all pass; none converted to absent, no tool run repeated ([summary](failure-injection.json)) |
| Orphan recovery | Dead worker's scan stopped 5.1 s after worker and SpiderFoot API recovered (gate 60 s) |
| Workflow | Thorough email → GHunt/SpiderFoot → discovered handle → Maigret all 5,178 sites within the shared budget (staging and production acceptance) |
| Live parity | 12/12 invocations, none interrupted or repeated, retention 12/12. GHunt matches; SpiderFoot identical findings, only per-scan `generated` timestamps differ; Maigret round 2 differs on nondeterministic sites — reviewed, no adapter loss (ledger `live_parity`) |
| Search | Canary ready via DuckDuckGo/Bing; Brave rate-limited and Bing intermittently irrelevant stay open incidents |
| Soak | 24 h on the final image: 2,784 rounds, 0 failures, API max 67 ms, RSS ≤ 66 MB, DB 107 MB ([report](soak-final.json)) |
| Restore | Pre-release archive with final image: 8.5 s, schema 4, 525 blobs, 371 SpiderFoot events ([report](restore-drill-final.json)) |
| Paired rollback | Previous images on the pre-release archive: 12/12 checks, old code refuses schema 4 ([report](rollback-drill-final.json), [script](rollback_drill.py)) |
| Deployment | `verify-deployment.sh` 52/52, `/ready` all true, monitor confined, no orphans at start, incident timer enabled |
| Acceptance | Production root `0b8d79c8…` + follow-up `45bdfd78…`: SpiderFoot 1,439 checks measured, Maigret 5,178 accounted, status partial (robots, provider degradation) ([summary](acceptance.json)) |

Defects found during validation and fixed with regression tests: `verify_upgrade.py` rebuilt
replay specs without the investigation (false idempotency failure on production data), and
SpiderFoot per-site checks were replayed newest-first so every site stayed `unfinished`.

## Known limitations

- A SpiderFoot PostgreSQL restart while a scan is starting leaves that scan `STARTING`; Harvest
  stops it at the 900 s ceiling and records partial. Interrupted SpiderFoot runs keep their
  partial events in SpiderFoot only (referenced by scan id).
- Maigret reports dead-proxy failures as `unfinished` (no native verdict), not `errored`; several
  Maigret site definitions flip between runs. Operator-cancel execution reasons read "worker stopped".
- Readiness sees GHunt credential presence, not validity. GHunt 2.3.4 name/Maps detail unsupported.
- Live parity ran on build `f15bc829…` (ceiling spent); its compared code is identical to the deployed build.
- `scripts/pin_release.py` defaults `--spiderfoot-frontend` to `sf-frontend`; this host needs `sf-frontend-ui`.
- Tool traffic and proxy spend are outside Harvest request and dollar budgets.

## Operator handoff

- Daily backup 03:00 UTC on OVH (installed script now includes readiness/release files); hourly
  pull and verification on the workstation; `/ready` fails if either is older than 24 h.
- Desktop incident alerts: `systemctl --user status harvest-incidents.timer` on the workstation.
- Verify after any change: `cd /opt/harvest/app && ./verify-deployment.sh`, then authenticated `/ready`.
- Pins live in write-once `/opt/harvest/app/.env.release`, mirrored into `.env` and
  `spiderfoot-ng/.env`; always roll out with `--no-build --pull never`.
- Rollback: stop api/worker/monitor, restore the pre-release archive into fresh volumes with the
  interrupted-job rule, restore `*.bak.prerelease-20261008T2010Z` configuration, start the
  previous images listed above. Never run the previous image on the schema-4 database.
- Release evidence that names sites or targets stays private: `/opt/harvest/releases/` on OVH and
  `/home/pop/backups/harvest/release-evidence/` on the workstation.

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

Use `scripts/pin_release.py --spiderfoot-frontend sf-frontend-ui` on the deployment host to resolve tested builds and unchanged
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
credentials, networks and API access. Keep acquisition stopped until checks pass. The isolated paired rollback drill passed on the pre-release archive (rollback-drill-final.json).

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
than inferring success from missing errors. The patched scanner's live log path is validated:
production acceptance measured all 1,439 `sfp_accounts` site checks. Other modules remain
`unsupported` per check. TLS-incompatible sites fail checks. Active-job restore semantics are
proven by the final-image failure-injection run (backup taken mid-execution).
