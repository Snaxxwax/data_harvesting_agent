# Harvest OSINT stack — ovh-vps

Deployed commit: `4fc1c2b` (data_harvesting_agent main, "Merge pull request #9 from
Snaxxwax/feat/proxy-only-egress"). Checkout `/opt/harvest/app`, overrides in
`compose.override.yaml` and the opt-in `compose.egress-proxy.yaml` (both tracked in the
repo under `deploy/ovh-vps/`).

SpiderFoot NG: `/opt/harvest/spiderfoot-ng`, poppopjmp/spiderfoot **v6.1.0**, based on
upstream `4b53ca68ea63548c25c4148a3a18bda8d9417c74`, **now patched**: deployed commit
`36de167e`, branch `fix/auth-db-reconnect`, pushed to
`https://github.com/Snaxxwax/spiderfoot` (the `patched` remote in that checkout). Three
commits: two fixing connection recovery (see "Postgres restart recovery" below) and one
attaching `sf-api` to the `harvest-egress` network for proxy-only mode.

Last verified: 2026-10-02.

## Access

Everything is Tailscale-only, on `100.118.181.47` / `ovh-vps.takaya-pound.ts.net`.

| Service | URL | Auth |
|---|---|---|
| Harvest API + web UI | http://100.118.181.47:8000 | bearer token (`HARVEST_API_TOKEN`) |
| SpiderFoot NG API | http://100.118.181.47:8001 | `X-API-Key` (`SPIDERFOOT_NG_SERVICE_KEY`) or admin JWT |
| SpiderFoot NG UI | http://100.118.181.47:3000 | admin / `SPIDERFOOT_NG_ADMIN_PASSWORD` |
| FlareSolverr | *internal only* | none; `http://flaresolverr:8191` on the compose network |

Ports are published **bound to the Tailscale address**, not `0.0.0.0`. Docker writes its
own iptables rules into `DOCKER-USER` that bypass ufw, so the interface binding — not the
ufw rule — is what keeps these off the public internet. Public tcp/22 is closed; SSH is
Tailscale-only.

Credentials: `/opt/harvest/credentials.env` and `/opt/harvest/app/.env`,
`/opt/harvest/spiderfoot-ng/.env`. All mode 600, all gitignored.

## SpiderFoot NG (replaced v4)

Minimal core only: `redis`, `postgres`, `api`, `celery-worker`, `frontend`. The optional
`storage` (MinIO/Qdrant/Tika), `ai`, `monitor`, `scan`, `sso`, `scheduler` and `proxy`
profiles are **not** included — `compose.core.yml` includes only `docker/compose/core.yml`.
Logs mention MinIO being unset; that is the disabled storage profile degrading gracefully.

Sized for 6 cores / 11 GiB shared with Harvest: API 1 CPU / 1 GiB with 2 uvicorn workers,
celery 1.5 CPU / 1.5 GiB, frontend 0.25 / 128 MiB, postgres 1 / 768 MiB, redis 0.5 /
256 MiB. Postgres and redis limits are added here; upstream leaves them unbounded, which
on a shared host means the whole 11 GiB.

### Provisioning an API key — important

**Do not use `POST /api/v1/keys`.** That endpoint mints `sfk_`-prefixed keys into Redis,
while the auth middleware validates `sf_`-prefixed keys out of Postgres `tbl_api_keys`.
Keys from the documented endpoint can never authenticate. Reported upstream as
poppopjmp/spiderfoot#396.

Mint through `AuthService` instead, which writes to the table that is actually validated:

    docker exec -i sf-api python - <<'PY'
    from spiderfoot.auth.service import get_auth_service
    svc = get_auth_service()
    admin = svc.get_user_by_username("admin")
    api_key, raw = svc.create_api_key(user_id=admin.id, name="harvest-integration",
                                      role="operator", expires_at=0.0)
    print(raw)
    PY

`SF_API_KEY_SECRET` must stay set in `.env`. Upstream's `core.yml` does not pass it
through; unset, the key manager generates a random HMAC secret per process and cannot
persist it (read-only filesystem), so keys would not survive a restart and the two uvicorn
workers would disagree about validity. The override passes it explicitly and fails the
config if it is missing.

### Postgres restart recovery — fixed 2026-10-02

Previously the API did not recover from a Postgres restart: it kept a stale connection and
answered `401 Invalid or expired API key` — not a 5xx — until `sf-api` was restarted by
hand. Two separate defects, both now patched in the fork:

**1. The connection was never validated before reuse.** `AuthService` caches one psycopg2
connection for the life of the process. After Postgres restarts, libpq does not notice
until it next attempts I/O: `conn.closed` is still `0` and the transaction status is still
`IDLE`, so *only a real query reveals it*. All ~35 call sites in that class then failed
permanently. `_get_conn` now pre-pings (`SELECT 1`) before handing the connection back and
reconnects when the ping fails — one guard covering every caller. It also rolls back a
connection stranded in an aborted transaction, which `autocommit = False` otherwise makes
permanent after a single failed statement.

A subtlety worth keeping: the ping must run *even when a transaction is open*. Reads here
never commit, so `INTRANS` is the normal resting state between requests; skipping the ping
for it skipped exactly the request that needed it. A `SELECT 1` inside an open transaction
commits and discards nothing. Only `ACTIVE` — a statement genuinely executing — is left
untouched.

**2. A database outage was reported as a bad credential.** The middleware wrapped both
credential paths in a bare `except Exception` and answered `401`. DB failures now raise
`AuthBackendUnavailable` and the middleware maps it to **`503` + `Retry-After`** on both
the API-key and the JWT path. The JWT path had the identical bug.

The same stale-connection flaw existed one layer down: psycopg2's `ThreadedConnectionPool`
does not validate on `getconn()`, so `DbCore` handed out dead sockets and the first request
after a restart returned `500 Failed to create DB handle: ... cursor already closed`. Dead
connections are now pre-pinged on checkout and returned with `close=True` so the pool
replaces them.

Verified on this host: warm both API workers, `docker restart sf-postgres`, then **12/12
authenticated requests returned 200** — no 401, no 500, no 503. During a genuine outage
(`docker stop sf-postgres`) every request is `503 AUTH_BACKEND_UNAVAILABLE` with
`retry-after: 5`, and a bad key returns to `401` once Postgres is back.

**Note what the healthcheck cannot do.** `/health` opens its own connection, while
`AuthService` caches a separate one, so during this bug `/health` reported postgresql `up`
while every authenticated call 401'd. No HTTP probe can observe another component's private
connection — this had to be fixed in code. The override still replaces upstream's
`/api/docs` probe (which touches no DB at all) with a `/health` probe asserting
`components.postgresql.status`, which is strictly better but was never going to catch this.

### Celery healthcheck — fixed 2026-10-02

`sf-celery-worker` reported `unhealthy` with a failing streak of 30 while answering `pong`
perfectly. Upstream's probe is
`celery -A spiderfoot.celery_app:celery_app inspect ping --timeout 5`. Importing
`spiderfoot.celery_app` in a cold process costs **~5.0s** here, and `inspect ping` waits
out the full reply window rather than returning on the first reply, so the probe needed
**~10.1s against Docker's 10s timeout** — killed every single time.

`-b/--broker` pings over the broker directly and skips the spiderfoot import entirely,
which is the whole 5s. Measured here: **~4.4s and exit 0** when the worker is up, ~4.3s
with no worker and ~8.5s with the broker unreachable (**exit 69** in both cases) — so it
still reports unhealthy for the real failures, which is the only reason to keep a probe.

### v4 retirement

v4 is stopped and its service removed from `compose.override.yaml`, so `up -d` will not
bring it back. **Its data is preserved**: volume `harvest-platform_spiderfoot-data` still
holds `spiderfoot.db`, its scan history and its `passwd` file, and the image
`spiderfoot:v4.0-local` is retained. Restore the service block from this file's git history
to bring it back. Delete that volume only deliberately.

## Harvest → SpiderFoot integration

Harvest allowlists `spiderfoot` as a tool (`HARVEST_TOOLS=maigret,spiderfoot`) and reaches
it at `http://sf-api:8001` over the shared external Docker network `osint-net`, never a
public address. It creates a scan, polls to `FINISHED`, and stores the events as one
capture (`tool://spiderfoot/<target>`).

`HARVEST_SPIDERFOOT_MODULES=sfp_dnsresolve` — one passive resolver. SpiderFoot's full
module set is active reconnaissance against the target, which is an authorization decision
that has to be made deliberately. A scan ending in any state other than `FINISHED` fails
the task rather than storing a partial result set.

## maigret

`--all-sites` and `--no-autoupdate` are both in effect. `--no-autoupdate` is now
unconditional (it used to be passed only alongside a proxy), so the site database is pinned
to the image: no unbudgeted GitHub request precedes a scan, and a scan's breadth cannot
change under the deployment without the image changing. Refresh it by rebuilding.

`--all-sites` is roughly ten times the outbound requests of a default scan, none of it
inside the fetcher's budgets or per-origin pacing.

### Egress — read before an engagement

**CURRENT STATE: no proxy. `HARVEST_EGRESS_PROXY` is unset and `HARVEST_EGRESS_MODE` is
`direct`,** so maigret and SpiderFoot reach every target **directly from this VPS's public
address, `40.160.89.118`**. An `--all-sites` run is roughly 6,000 distinct destinations of
conspicuous traffic attributable to that address. Treat it as an authorization question,
not only a volume one.

Proxy-only mode is **built, tested and ready**; it is not enabled because no upstream proxy
exists yet. See "Enabling proxy-only egress" below.

#### Why setting a proxy is not enough

Each tool reaches the network differently, and two paths ignore `--proxy` outright:

| Path | Proxy support | Behaviour in `proxy` mode |
| --- | --- | --- |
| Harvest fetcher (`httpx`) | Full; `trust_env=False`, so no ambient proxy is ever used | Proxied, restricted to `HARVEST_PROXY_PUBLIC_HOSTS` |
| maigret site checks | `--proxy` | Proxied (verified: 5,987 distinct destinations through the relay) |
| maigret site-DB update | **none** — ignores `--proxy` | Disabled unconditionally (`--no-autoupdate`); DB pinned to the image |
| maigret activation helpers | **none** — separate `ClientSession`s upstream | Unfixable in-process → mode requires *and verifies* network-level blocking |
| SpiderFoot HTTP modules | its own global `_socks*` config, own container | Scan refused unless SpiderFoot's proxy matches `HARVEST_EGRESS_PROXY` |
| **SpiderFoot DNS modules** | **none — an HTTP proxy cannot carry DNS** | **Still direct. See the limitation below.** |
| FlareSolverr / CF bypass | own container, not covered by `--proxy` | Refused outright |

`HARVEST_PROXY_PUBLIC_HOSTS` governs the fetcher only, never maigret's site list.

#### How proxy-only mode cannot silently fall back

Two independent mechanisms, because an application-level flag alone is not a guarantee:

**1. No route (structural).** The worker joins only `harvest-egress`, an *internal* Docker
network with no gateway. It has no route to the internet at all; the relay is the only way
out. A forgotten firewall rule silently restores direct egress — a missing route cannot.
Verified: a raw TCP connect to `1.1.1.1:443` from the worker fails, while the same connect
from a normal bridge network succeeds.

**2. A probe, not an honour-system flag.** Because maigret's activation helpers cannot be
fixed from inside Harvest, proxy mode does not take an operator's word that a firewall
exists — before running maigret it opens a plain TCP connection to `HARVEST_EGRESS_PROBE`
(default `1.1.1.1:443`) with no proxy. **If that connects, the tool refuses to run.** A
blocked probe is the pass condition. It tests one destination, so it detects an absent or
ineffective policy rather than proving a correct one; that is why mechanism 1 carries the
actual guarantee.

SpiderFoot is gated differently, since it egresses from its own container: Harvest reads
back `_socks1type`/`_socks2addr`/`_socks3port` over SpiderFoot's API and refuses the scan
unless they match `HARVEST_EGRESS_PROXY`. Verified both ways — with SpiderFoot's proxy
unset the job is **blocked** (`"HARVEST_EGRESS_MODE=proxy but SpiderFoot's global proxy
does not match..."`), and with it pointed at the relay the same job **completed** with 72
claims.

#### Limitations — what proxy-only mode does NOT cover

Measured on this host, not assumed:

1. **DNS is not proxied, for anything.** An HTTP proxy cannot carry DNS. SpiderFoot's
   default module is `sfp_dnsresolve`, which resolves through the system resolver: `sf-api`
   resolved `example.org` directly in testing. DNS queries therefore still disclose the
   target to the resolver and this VPS's address to authoritative servers. A SOCKS5 proxy
   does not fix this either, because those modules do not route DNS through it. To avoid
   DNS exposure you must restrict SpiderFoot to non-DNS modules, which removes most of its
   value, or place a DNS forwarder behind the proxy. **Unresolved.**
2. **The SpiderFoot containers are not confined.** `sf-api` and `sf-celery-worker` keep
   internet-capable networks and *can* egress directly (verified reachable). The `_socks*`
   setting is honoured by its HTTP modules but nothing enforces it, and Harvest's gate
   checks the configuration, not the packets. Confining them the way the worker is confined
   would break DNS resolution and stop SpiderFoot working at all.
3. **The probe covers the worker only.** It says nothing about the SpiderFoot or
   FlareSolverr containers, which have their own networks.
4. **In proxy mode the fetcher denies any destination not in
   `HARVEST_PROXY_PUBLIC_HOSTS`.** Crawling maigret's discovered leads therefore requires
   naming those hosts explicitly; otherwise tool results are stored but leads are not
   followed.

#### Enabling proxy-only egress

Needs the upstream proxy credentials (Webshare US static ISP — HTTP with
`user:password@host:port`, which `HARVEST_EGRESS_PROXY` and the relay both already
support). Create the internal network once — outside both compose projects, so neither
stack depends on the other's startup order:

    docker network create --internal harvest-egress

Set the `Upstream` line in `/opt/harvest/egress-relay/tinyproxy.conf`:

    Upstream http USER:PASSWORD@HOST:PORT

Then bring the stack up with the opt-in file:

    cd /opt/harvest/app
    set -a; . .env.build; set +a
    docker compose -f compose.yaml -f compose.override.yaml \
                   -f compose.egress-proxy.yaml up -d

And point SpiderFoot at the same relay (note the `options` wrapper — a bare body is
rejected with `422 Field required: body → options`):

    curl -X PATCH -H "X-API-Key: $SPIDERFOOT_NG_SERVICE_KEY" \
      -H 'content-type: application/json' \
      http://100.118.181.47:8001/api/v1/config \
      -d '{"options":{"_socks1type":"HTTP","_socks2addr":"egress-relay","_socks3port":"8888"}}'

Then confirm, rather than assume:

    # must print BLOCKED
    docker exec harvest-platform-worker-1 python -c \
      "import socket;socket.create_connection(('1.1.1.1',443),timeout=5)" \
      && echo REACHABLE || echo BLOCKED
    # must print the real exit IP of the proxy, not 40.160.89.118
    docker exec harvest-platform-worker-1 python -c \
      "import httpx;print(httpx.get('https://api.ipify.org',proxy='http://egress-relay:8888',trust_env=False,timeout=30).text)"

**Reverting:** bring the stack up without `-f compose.egress-proxy.yaml`, and reset
SpiderFoot's `_socks*` to empty strings — otherwise its modules point at a relay that no
longer exists.

#### Verification performed 2026-10-02

With a local tinyproxy relay chained to a second, credential-requiring proxy standing in
for Webshare (same shape: HTTP proxy, `user:pass@host:port`):

- Worker confined to `harvest-egress`: canary to `1.1.1.1:443` **blocked**; `sf-api:8001`
  still reachable; `https://example.org` through the relay **200**.
- `Settings().proxy_only` true and the egress check **passed** (direct egress blocked).
- maigret job in proxy-only mode: **203 records**, and the relay logged **5,987 distinct
  destinations** with the credentialed upstream carrying 6,508 requests — proof the site
  sweep actually traversed the proxy chain rather than merely succeeding.
- SpiderFoot gate: **blocked** with its proxy unset, **completed** (72 claims) with it set.
- Reverted to direct mode afterwards; final regression check — maigret 209 records,
  SpiderFoot completed with 8 records.

Only the relay's `Upstream` line changes for the real Webshare endpoint.

## Cloudflare bypass — still disabled

`HARVEST_MAIGRET_CLOUDFLARE_BYPASS=false`. FlareSolverr runs but is **not** wired to
maigret:

1. maigret 0.6.6 expects a bypass service at `localhost:8191` *inside the worker
   container*; FlareSolverr is a separate container. The flag alone would not reach it, and
   maigret's `settings.json` lives inside a read-only image.
2. FlareSolverr drives a real browser and its egress is not covered by
   `HARVEST_EGRESS_PROXY`, so it must be verified separately.

To enable later: verify FlareSolverr's egress, make it reachable from the worker, repoint
maigret's `settings.json`, then flip the flag.

## Operating

    cd /opt/harvest/app          && docker compose ps
    cd /opt/harvest/spiderfoot-ng && docker compose -f compose.core.yml ps

Rebuild Harvest after a repo update:

    git -C /opt/harvest/app fetch origin && git -C /opt/harvest/app checkout <commit>
    cd /opt/harvest/app && HARVEST_TOOL_PACKAGES="maigret==0.6.6" docker compose up -d --build

Rebuild SpiderFoot NG. **The base image is not optional.** All the Python source lives
in `spiderfoot-base`; `Dockerfile.api` and `Dockerfile.scanner` only add configuration on
top of it. `docker compose build api` therefore reports "Built" and changes nothing —
it reuses a cached layer and silently ships the old code. Build the base first, then
verify the change is actually in the image before deploying:

    cd /opt/harvest/spiderfoot-ng
    docker build -f docker/Dockerfile.base -t spiderfoot-base:latest .
    docker compose -f compose.core.yml --env-file .env build api celery-worker
    # confirm, do not assume:
    docker run --rm --entrypoint grep spiderfoot-ng-api:latest -c _conn_is_usable \
      /home/spiderfoot/spiderfoot/auth/service.py
    docker compose -f compose.core.yml --env-file .env up -d

### Fork patches

The checkout at `/opt/harvest/spiderfoot-ng` has two remotes: `origin`
(poppopjmp/spiderfoot, upstream) and `patched` (Snaxxwax/spiderfoot). The deployed branch
`fix/auth-db-reconnect` (`36de167e`) is pushed to `patched`, so the patches are
reproducible from GitHub rather than existing only on this host:

    git -C /opt/harvest/spiderfoot-ng log --oneline 4b53ca68..fix/auth-db-reconnect

That host has no GitHub credentials, so pushes are moved off it with a bundle:

    git -C /opt/harvest/spiderfoot-ng bundle create /tmp/fork.bundle \
      4b53ca68..fix/auth-db-reconnect
    # then from a machine with credentials:
    #   git fetch /path/to/fork.bundle 'refs/heads/*:refs/heads/*' && git push patched <branch>

Rebasing onto a newer upstream: the patches touch `spiderfoot/auth/service.py`,
`spiderfoot/auth/middleware.py`, `spiderfoot/auth/models.py` and
`spiderfoot/db/db_core.py`, plus `test/unit/test_auth_db_recovery.py` and
`test/unit/test_db_pool_recovery.py` (13 tests, all passing). Run those two files first
after any rebase.

Note for the test suite: `test/unit/test_db_migrate.py::TestMigrationManagerWithPostgres`
fails 16/16 on **stock upstream too** (it passes a SQLite path to `PostgresAdapter`).
That is pre-existing, not caused by these patches — verified against a pristine
`4b53ca68` checkout.
