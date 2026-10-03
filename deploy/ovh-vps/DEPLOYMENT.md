# Harvest OSINT stack — ovh-vps

Deployed commit: `aa8efd3` (data_harvesting_agent main), deployed 2026-10-03. It merges
`feat/ghunt-tool` (ghunt implemented, **not** enabled — see "ghunt" below) and
`feat/tool-evidence-quality` (tool evidence reaches dossiers, graded confidence, crawl
relevance). Checkout `/opt/harvest/app`, overrides in `compose.override.yaml` and the opt-in
`compose.egress-proxy.yaml` and `compose.ghunt.yaml` — all tracked in the repo under
`deploy/ovh-vps/`.

**The running stack loads `compose.egress-proxy.yaml`, so it is in proxy mode.** Bringing it
up without that file recreates both containers in `direct` mode, silently, because
`HARVEST_EGRESS_*` is defined only in that overlay and not in `.env`. Always use the full
three-file invocation in "Operating" below.

SpiderFoot NG: `/opt/harvest/spiderfoot-ng`, poppopjmp/spiderfoot **v6.1.0**, based on
upstream `4b53ca68ea63548c25c4148a3a18bda8d9417c74`, **now patched**: deployed commit
`daee4b5f`, branch `fix/auth-db-reconnect`, pushed to
`https://github.com/Snaxxwax/spiderfoot` (the `patched` remote in that checkout). Four
commits: two fixing connection recovery (see "Postgres restart recovery" below) and two
on the `harvest-egress` network wiring for proxy-only mode.

Last verified: 2026-10-03.

### Verified on the deployed commit (2026-10-03)

Both checks spend no full sweep: one is an offline replay, the other a 25-site scan.

| Check | Result |
| --- | --- |
| Saved capture 212 (`tool://maigret/Snaxxwax`) replayed with a tool source rule | **20 accounts admitted, 268 claims, `excluded: []`** — before this commit the same replay produced an empty "completed" dossier |
| Live 25-site scan, `crawl: false` | `account_id` 105263527 and `account_created` 2022-05-10 promoted out of `status.ids` as their own sourced claims; confidence 0.9; `GitHubGist` merged into `GitHub` as `merged_hosts`; no sentinels, no `{username}` templates, 0 fetch tasks queued, `suppressed_leads` 2 (was always reported as 0) |
| Egress posture | worker direct connect to `1.1.1.1:443` fails; the scan made 41 upstream connections through the relay, none direct |

Note on the replay: capture 212's **body is immutable evidence**, recorded before this
commit, so its claims still carry confidence 1.0 and no promoted fields. Replaying it proves
the dossier-admission fix; grading and reshaping apply to captures taken from now on.

Ground truth: only `https://github.com/Snaxxwax` is a confirmed account. The other 19 in the
replay are **unverified handle matches** and the dossier says so — each is labelled
"account matched on identifier overlap, identity not verified".

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

**CURRENT STATE (verified 2026-10-03): proxy mode.** The running containers are up with
`compose.egress-proxy.yaml`, so `HARVEST_EGRESS_MODE=proxy`,
`HARVEST_EGRESS_PROXY=http://egress-relay:8888`, and the relay chains to the upstream
Webshare endpoint. Verified on the deployed commit: a raw TCP connect to `1.1.1.1:443` from
the worker **fails** (it is on the internal-only `harvest-egress` network), and a 25-site
maigret scan produced **41 upstream connections through the relay** and none direct.

This section previously read "CURRENT STATE: no proxy ... `direct`", which was stale and
actively misleading: the "Operating" rebuild command right below it has always included
`-f compose.egress-proxy.yaml`, so a documented rebuild brings the stack up in **proxy**
mode. Anyone trusting the old sentence and omitting that file silently moved the deployment
to direct egress. It happened once during the 2026-10-03 deploy — one 25-site verification
scan went out directly from `40.160.89.118` before the posture was restored.

If you deliberately revert to `direct` mode, maigret and SpiderFoot reach every target
**directly from this VPS's public address, `40.160.89.118`**. An `--all-sites` run is
roughly 6,000 distinct destinations of conspicuous traffic attributable to that address.
Treat it as an authorization question, not only a volume one.

**Bandwidth.** The upstream allowance is 1 GB/month and a full `--all-sites` sweep costs
roughly 41–50 MiB, so about twenty full sweeps per month. Prefer replaying a saved capture
(`POST /replays`, no egress at all) or `ToolRun.top_sites` for a smaller scan; the 25-site
verification above cost well under 1 MiB.

#### Why setting a proxy is not enough

Each tool reaches the network differently, and two paths ignore `--proxy` outright:

| Path | Proxy support | Behaviour in `proxy` mode |
| --- | --- | --- |
| Harvest fetcher (`httpx`) | Full; `trust_env=False`, so no ambient proxy is ever used | Proxied, restricted to the job's `allowed_domains` + `require_public_host()`, and to `HARVEST_PROXY_PUBLIC_HOSTS` when that is non-empty |
| maigret site checks | `--proxy` | Proxied (verified: 5,987 distinct destinations through the relay) |
| maigret site-DB update | **none** — ignores `--proxy` | Disabled unconditionally (`--no-autoupdate`); DB pinned to the image |
| maigret activation helpers | **none** — separate `ClientSession`s upstream | Unfixable in-process → mode requires *and verifies* network-level blocking |
| SpiderFoot HTTP modules | its own global `_socks*` config, own container | Scan refused unless SpiderFoot's proxy matches `HARVEST_EGRESS_PROXY` |
| **SpiderFoot DNS modules** | **none — an HTTP proxy cannot carry DNS** | **Still direct. See the limitation below.** |
| FlareSolverr / CF bypass | own container, not covered by `--proxy` | Refused outright |

`HARVEST_PROXY_PUBLIC_HOSTS` governs the fetcher only, never maigret's site list. It is an
*optional* operator restriction: exact hostnames, no wildcards, no subdomain matching.
Empty (the default) means "no operator allowlist", not "deny everything".

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

1. **DNS is not proxied, for anything — ACCEPTED RISK, decided 2026-10-02.** An HTTP
   proxy cannot carry DNS. SpiderFoot's default module is `sfp_dnsresolve`, which resolves
   through the system resolver: `sf-api` resolved `example.org` directly in testing. DNS
   queries therefore still disclose the target to the resolver, and this VPS's address to
   authoritative servers, **even with proxy-only mode fully enabled**. A SOCKS5 proxy does
   not fix it either, because those modules do not route DNS through it.

   The options were: accept it; put a DNS-over-TLS forwarder behind the proxy; restrict
   SpiderFoot to non-DNS modules (which removes most of its value, `sfp_dnsresolve` being
   the default); or drop SpiderFoot from proxy-only engagements. **Decision: accept and
   document**, keeping SpiderFoot's full function. Treat DNS-level attribution as in scope
   for any engagement run from this host, and revisit if an engagement makes it
   disqualifying.
2. **The SpiderFoot containers are not confined.** `sf-api` and `sf-celery-worker` keep
   internet-capable networks and *can* egress directly (verified reachable). The `_socks*`
   setting is honoured by its HTTP modules but nothing enforces it, and Harvest's gate
   checks the configuration, not the packets. Confining them the way the worker is confined
   would break DNS resolution and stop SpiderFoot working at all.
3. **The probe covers the worker only.** It says nothing about the SpiderFoot or
   FlareSolverr containers, which have their own networks.
4. **`HARVEST_PROXY_PUBLIC_HOSTS`, when set, is an exact-hostname allowlist.** It admits
   no wildcards and does not match subdomains, so `example.gov` does not cover
   `www.example.gov`. Setting it is incompatible with open-ended discovery: maigret's
   leads land on hosts nobody can enumerate in advance. Leave it empty unless the
   deployment genuinely crawls a fixed set of hosts.
5. **The guard cannot see through DNS.** See "Proxy-mode SSRF boundary" below.

#### Proxy-mode SSRF boundary

`require_public_host()` checks the **shape** of a destination, never what it resolves to.
It rejects non-global IP literals (`10.0.0.5`, `127.0.0.1`, `169.254.169.254`,
IPv4-mapped v6), the suffixes `.localhost .local .internal .intranet .lan .corp
.home.arpa`, and single-label names. `canonical_url()` has already rejected non-HTTP
schemes and embedded credentials before the guard runs, and `raw_get()` re-guards every
redirect hop, so a 302 into private space is denied rather than followed.

**What it does not cover:** a *public* hostname that resolves to a *private* address —
DNS rebinding, split-horizon DNS, or a wildcard service such as `10.0.0.5.nip.io`. This
is structural, not an oversight. In proxy mode the proxy performs DNS; the worker has no
resolver at all (its network is `internal: true`, so name lookups fail with `gaierror`),
which is exactly why `resolved_addresses()` — the check that guards direct mode — cannot
run. Verified: `require_public_host("10.0.0.5.nip.io")` returns cleanly.

What contains it instead is the egress path, in three layers:

1. The worker has no route off-host except the relay — its network has no gateway.
2. The relay, with a blanket `Upstream`, never connects to the destination itself. It
   opens a socket to the upstream proxy and forwards the request; the relay logs show
   `Found upstream proxy ... for 10.0.0.5.nip.io` followed by a connection to the
   upstream address only.
3. The upstream proxy refuses private destinations. Webshare answers **403 Forbidden**
   for all of `10.0.0.5.nip.io`, `172.18.0.1.nip.io`, `127.0.0.1.nip.io` and
   `169.254.169.254.nip.io`.

Layer 3 is a third party's policy, so treat it as a mitigation, not a guarantee.

**Residual risk, precisely.** If the `Upstream` line is removed or mistyped, tinyproxy
resolves and connects directly, and layers 2 and 3 are both gone. Measured from the relay
in that position: the VPS host (`172.18.0.1:8000`, `:80`) and the Tailscale address
(`100.118.181.47:8000`) are **unreachable** — the host firewall denies the Docker subnet —
but a **sibling container on `harvest-platform_default` is reachable** (`172.18.0.2:8000`,
the Harvest API). So a rebinding name aimed at a sibling container's address would resolve
and connect in that misconfigured state. The entrypoint now refuses to start without a
credential, which removes the "removed" half of this risk; after any change to the secret
or the template, still confirm the relay egresses through the upstream:

    docker exec harvest-platform-worker-1 python -c "import urllib.request as u; \
      print(u.build_opener(u.ProxyHandler({'https':'http://egress-relay:8888'})) \
      .open('https://ipv4.webshare.io/',timeout=30).read().decode())"

That must print the upstream proxy's address, never the VPS public address.

#### Upstream proxy credential

**The credential is not in any tracked file, any command argument, or the container's
environment.** It lives in one place on the host:

    /opt/harvest/egress-relay/upstream.secret     # 0600 root:root, untracked, OUTSIDE the repo
    HARVEST_EGRESS_UPSTREAM=user:password@host:port

`egress-relay/entrypoint.sh` reads that file at container start, appends the `Upstream` line
to the tracked template, and writes the assembled config to a **tmpfs** at
`/run/tinyproxy/tinyproxy.conf` (mode 600), then `exec`s `tinyproxy -d -c` on it. So the only
at-rest copy inside the container is in memory and disappears with the container.

It used to be a literal `Upstream http user:pass@host:port` line in
`/opt/harvest/egress-relay/tinyproxy.conf`, **mode 0644** — readable by every user on the
host, and trivially captured by anything that `cat`s the file, which is how it leaked into a
work transcript on 2026-10-03.

Why each exposure route is closed, and not just the tracked-file one:

| Route | Why it is a problem | How it is avoided |
|---|---|---|
| tracked file | ends up on GitHub | template carries no `Upstream` line; `verify-deployment.sh` greps `deploy/` for a credentialed one |
| command argument | `/proc/<pid>/cmdline` is world-readable for the life of the process, and `ps` shows it | read with the `read` builtin, written with a heredoc — never `sed "s/X/$PASS/"` |
| environment | visible in `docker inspect`, `docker compose config`, `/proc/1/environ` | passed as a 0600 bind-mounted file, not an env var |
| logs | persists in the journal / json-file driver | nothing echoes the value; failure messages name only the path or the missing key; `LogLevel Info` does not log it |

Verified in the running container: `tinyproxy`'s argv is `tinyproxy -d -c
/run/tinyproxy/tinyproxy.conf`, `docker inspect` shows no credential-shaped value in
`Env`/`Cmd`/`Entrypoint`, and `docker logs` contains none.

**It fails closed, deliberately.** tinyproxy with no `Upstream` line does not error — it runs
and connects to destinations *directly from this VPS's address*, silently defeating
proxy-only egress while Harvest's own `HARVEST_EGRESS_PROBE` still passes (that probe proves
the **worker** has no route, not that the **relay** uses an upstream). So the entrypoint
refuses to start when the secret is missing, empty, or not in `user:password@host:port`
form, and `verify-deployment.sh` separately asserts the running config has an `Upstream`
line. Tested: all four bad-input cases exit 1 and print no credential.

The template is mounted at `/etc/tinyproxy/tinyproxy.conf.template`, **not** at tinyproxy's
default config path, so that mounting it as the real config — which would produce exactly
the silent-direct-egress state above — is not an easy mistake to make.

**Moving the credential did not rotate it.** See "Rotating the upstream credential".

#### Rotating the upstream credential

Storage and rotation are separate. The value that was in the 0644 config is the same value
now in the secret file, and it was exposed while that file was world-readable, so it should
be **rotated at the provider**:

1. Webshare dashboard → Proxy → Settings → reset the proxy password (or delete and recreate
   the proxy user). This is the only step that invalidates the old credential; nothing on
   this host can do it.
2. Update `/opt/harvest/egress-relay/upstream.secret` with the new value (keep mode 0600).
3. `docker compose up -d --force-recreate egress-relay` — the entrypoint reassembles the
   config from the new secret; no rebuild is needed, since nothing is baked into an image.
4. `./verify-deployment.sh`, then confirm the exit IP is the upstream's and not this VPS's.

Also delete any stale copy once rotation is done:
`/opt/harvest/egress-relay/tinyproxy.conf.bak` held the credential at 0644 as well.

#### Enabling proxy-only egress

Needs the upstream proxy credentials (Webshare US static ISP — HTTP with
`user:password@host:port`, which `HARVEST_EGRESS_PROXY` and the relay both already
support).

`compose.egress-proxy.yaml` creates the `harvest-egress` network itself, so it exists
exactly when proxy mode is in use. **SpiderFoot deliberately does not declare that
network**, so its absence can never stop sf-api from starting — verified by removing the
network entirely and recreating sf-api, which came up healthy. The trade-off is that
sf-api must be attached manually at enable time (step 3 below).

Write the credential to the untracked secret file (see the next subsection for why it
lives there rather than in the config):

    install -m 600 -o root -g root /dev/null /opt/harvest/egress-relay/upstream.secret
    # then, without putting the value in your shell history or in a command argument:
    #   printf 'HARVEST_EGRESS_UPSTREAM=user:password@host:port\n' >> ...upstream.secret
    # or edit it with `install -m 600` already applied and no other process watching.

Then bring the stack up with the opt-in file:

    cd /opt/harvest/app
    docker compose up -d        # COMPOSE_FILE in .env applies every overlay

Attach sf-api to the internal network so the confined worker can still reach it. This
survives restarts but **not** a container recreate, so redo it after rebuilding sf-api:

    docker network connect harvest-egress sf-api

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
longer exists:

    curl -X PATCH -H "X-API-Key: $SPIDERFOOT_NG_SERVICE_KEY" \
      -H 'content-type: application/json' \
      http://100.118.181.47:8001/api/v1/config \
      -d '{"options":{"_socks1type":"","_socks2addr":"","_socks3port":""}}'

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

Only `/opt/harvest/egress-relay/upstream.secret` changes for the real Webshare endpoint;
the tracked template never carries a credential.

## ghunt

`HARVEST_TOOLS` on this host is `maigret,spiderfoot,ghunt`, the pin
`ghunt==2.3.4` is in `compose.override.yaml`'s `x-tools` anchor, and
`compose.ghunt.yaml` gives the worker a persistent writable `HOME`. All three are in place,
so the only remaining requirement is the credential:

`$HOME/.malfrats/ghunt/creds.m` inside the worker, where `$HOME` is `/home/harvest` on the
`harvest-ghunt-home` volume.

That step cannot be automated. GHunt authenticates as a real Google account and
`ghunt login` is interactive, so it needs a dedicated Google account the operator owns and
is willing to have attributed to this deployment's traffic. Harvest checks for the file
before launching the binary, so while it is absent an enabled ghunt reports
`ghunt has no credentials at /home/harvest/.malfrats/ghunt/creds.m` as a **policy denial**
(task status `blocked`) rather than a failed scan. `verify-deployment.sh` reports the
credential as present or absent and never reads it.

### Two upstream crashes, patched at build time

`ghunt email --json` — the exact invocation in `tools._ghunt` — **crashed every time on a
stock 2.3.4**, so ghunt could never complete a lookup before 2026-10-03. Two independent
bugs, both fixed on upstream `master` but not in any release; 2.3.4 is the newest on PyPI,
so there is no version to bump to. The `Dockerfile` patches both after `uv tool install`.

| # | File | Error | Scope |
|---|---|---|---|
| 1 | `ghunt/parsers/people.py` | `KeyError: 'container'` | any account with a cover photo |
| 2 | `ghunt/modules/email.py` | `NameError: name 'photos' is not defined` | **every** lookup |

**(1)** Google stopped returning `metadata.container` for `coverPhoto` entries. `photo` and
`readOnlyProfileInfo` still carry it, which is why only cover photos trip it — confirmed
against a live response, where `coverPhoto[0].metadata` holds `primary`, `visibility`,
`encodedContainerId` and `containerType`, but no `container`. The parser indexed it
unguarded and raised **before GHunt wrote any JSON**, so Harvest saw `ghunt exited 1` with
no report.

Upstream master fixes this with `.get("container", "unknown")`. **This deployment
deliberately does not copy that.** `tools._GHUNT_PROFILE_FIELDS` addresses the `PROFILE`
container, so keying cover photos under the literal `"unknown"` would turn a loud crash
into a silent mismapping — `cover_image_url` would stop erroring and simply never populate,
which is the worse outcome because nothing reports it. The same metadata object still
carries `containerType`, whose value *is* the real container name (`PROFILE`), so the patch
prefers `container`, then `containerType`, then `"unknown"`. That fixes the crash and keeps
the field addressable by the mapping that already exists. Verified in the resulting record:
`profile.coverPhotos.PROFILE.url` is present and `cover_image` reaches the dossier.

**(2)** The `--json` block references `photos` and `reviews`, but `gmaps.get_reviews`
returns only `(err, stats)` and neither name is ever assigned in `hunt()` — the four-value
returns that once supplied them are commented out a hundred lines up. This fires for every
`PROFILE` container regardless of the account, which is why `--json` never worked at all.
Upstream's fix (literal `None` for both) is copied verbatim; Harvest reads only the
`profile` container, so a null `maps.photos` is inert for every mapping.

Each patch sits behind a `grep` guard, so a ghunt release that rewrites either line **fails
the build** rather than shipping a binary that dies at run time on the first real target.
Because the patches live in a tracked `Dockerfile`, a redeploy that checks out an older
commit reverts them *and* their guards together — so `verify-deployment.sh` additionally
asserts that the patches are present in the **running image**, which is the only check that
survives that mistake.

### Name fields are permanently unavailable

**`fullname`, `first_name` and `last_name` can never populate, and that is upstream's
doing.** In 2.3.4 `PersonName._scrape` is a bare `pass`, with the comment *"Google patched
the names :/ very sad"*; the `displayName` / `givenName` / `familyName` reads are commented
out, so all three attributes keep their `""` initialisers for every account. GHunt's JSON
therefore contains `"names": {}`.

Three of the seven entries in `_GHUNT_PROFILE_FIELDS` are consequently dead. Harvest's
exact-type-plus-nonempty check degrades them to **field absent** rather than asserting an
empty name, which is the correct behaviour, so:

* a dossier mapping them lists `full_name` / `first_name` / `last_name` in its per-target
  `missing_fields` on **every** run — that is an accurate report, not a regression;
* they are kept in the mapping rather than deleted, so a GHunt release that restores name
  scraping begins populating them with no change on this side.

Do not treat a GHunt lookup as broken because it returned no name. Judge it on `gaia_id`,
`email`, `account_type`, `profile_image` and `cover_image`.

### Declaring `fields` on a job that uses `field_map`

`JobSpec.fields` names the fields **as the source produces them**, not as the dossier
renames them. For a tool source that is the record-side name the adapter emits — for ghunt:
`id`, `user_types`, `image_url`, `cover_image_url`, `profile_photo_is_default`, `email` —
and `field_map` then translates each one into its dossier name (`gaia_id`, `account_type`,
`profile_image`, `cover_image`, `default_photo`).

That is load-bearing, not cosmetic: reread gating in `Store.enqueue`, reasoning gating in
the engine, and the field list handed to the model all ask "has extraction produced this
field yet", so they must see the name extraction actually stores.

Declaring the dossier-side names instead used to make `GET /jobs/{id}` report every one of
them in `missing_fields` *while the dossier held values for all of them*. `Store.job` now
credits a declared field when an observation exists under either the record-side name or a
`field_map` entry that renames it, so the reported value matches what the dossier shows.
The widening is report-only — the gating consumers above still use the narrow comparison,
because a reread is justified precisely by a field the extractor has not produced yet.

It stays job-wide: it reports that the job produced a field somewhere, not that a given
target bound it. Per-target truth is the dossier's own `missing_fields`.

### Interactive login (one time)

    cd /opt/harvest/app
    ./ghunt-login.sh

Run it by hand, in your own terminal. The wrapper exists because two things are easy to get
wrong and both fail confusingly: GHunt reads `HTTP(S)_PROXY` from its own environment (its
`get_httpx_client()` takes no proxy argument), the worker container does not set those —
harvest injects them only into the tool subprocesses it spawns — and the worker is on an
internal network with no gateway, so without them the login cannot reach Google and the
error looks like a bad token rather than no route. `exec -T` would also kill the TTY the
prompts need.

GHunt offers four methods. **[1] (Companion listening mode) states it is "currently not
compatible with docker"** — it expects to bind a port on the browser's own machine. Use
**[2]** (paste the base64 blob from the GHunt Companion browser extension), **[3]** an
`oauth_token` (`oauth2_4/…`), or **[4]** a master token (`aas_et/…`).

Verified before handing this over: `android.googleapis.com`, `accounts.google.com` and
`people-pa.clients6.google.com` are all reachable through the relay, so the token exchange
has a route.

**GHunt echoes the OAuth2 token, then the account's name and email, to the terminal.** Those
lines are secrets — the master token it then saves is equivalent to a logged-in browser for
that account, is unscoped, and is revocable only by changing the password or signing out all
sessions. Do not copy that output into a chat, ticket, log or commit. The prompt echoes
nothing useful to a log, but **the resulting `creds.m` is a session credential**: it holds
that account's cookies, OSIDs and a long-lived Android master token, which together are
equivalent to a logged-in browser. It is not scoped and cannot be revoked except by changing
the account's password or signing out all its sessions. Never copy it out of the volume,
never bake it into an image, never commit it.

Then confirm it survives a restart, which is the whole point of the volume:

    docker compose restart worker
    docker compose exec worker test -f /home/harvest/.malfrats/ghunt/creds.m && echo present

### Exposure

Every `ghunt email` query is made **as that account**. Google sees the lookups and
associates them with it, so the account is attributable to this deployment's investigative
traffic and is plausibly subject to rate limiting or suspension for it. Use a dedicated
account, not a personal one.

Persistence is handled by the opt-in `compose.ghunt.yaml` (tracked under `deploy/ovh-vps/`),
which moves the worker's `HOME` to `/home/harvest` on a named volume. The base compose sets
`HOME=/tmp`, which is a tmpfs, and GHunt rewrites `creds.m` when it refreshes its session —
so without that overlay the credential would be destroyed on every restart and would have to
be obtained interactively again. See the header of that file for the one-time setup and the
restart check.

### Proxy posture

GHunt has no `--proxy` flag. Confirmed by reading 2.3.4: `helpers/utils.get_httpx_client()`
returns `AsyncClient(http2=True, timeout=15)` -- no proxy argument, `trust_env` left at its
default -- so `HTTP(S)_PROXY` is the only route it reads. (The source even carries a
commented-out line showing a proxy would otherwise have to be hardcoded.) Harvest strips
ambient proxy variables and re-injects the *validated* `HARVEST_EGRESS_PROXY` value.

The credential path is likewise confirmed on 2.3.4: `objects/base.GHuntCreds` builds it from
`Path().home() / ".malfrats/ghunt" / "creds.m"` with no override, and **creates that
directory if it is missing** -- so the home directory has to be writable as well as
persistent, which is what `compose.ghunt.yaml` provides.

| Path | Mechanism | Covered in proxy-only mode? |
|---|---|---|
| ghunt API calls | `HTTP(S)_PROXY` (no flag exists) | Env only — weaker than an argv flag |
| everything else | — | Mode requires *and verifies* network-level blocking |

Because an environment variable is a weaker promise than a flag, proxy-only mode keeps
requiring the host to block direct egress and verifies it with the same
`HARVEST_EGRESS_PROBE` TCP probe used for maigret. Enabling ghunt does not relax that.

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

Rebuild Harvest after a repo update. **Pass no `-f` flags.**

    git -C /opt/harvest/app fetch origin && git -C /opt/harvest/app checkout <commit>
    cd /opt/harvest/app
    cp -f deploy/ovh-vps/compose.*.yaml .      # overlays are tracked under deploy/
    docker compose build api worker
    docker compose up -d
    ./verify-deployment.sh                     # confirm, do not assume

`COMPOSE_FILE` in `.env` names all four compose files, so a bare `docker compose` applies
them all — including `compose.egress-proxy.yaml` (proxy mode) and `compose.override.yaml`
(the Tailscale-only port binding). **An explicit `-f` replaces that list entirely**, which
is how the previous version of this section caused an outage of posture rather than of
service: the command below it named three files, the prose above it claimed the deployment
was in direct mode, and anyone who typed a shorter command silently moved it there for real.

The external tool pin now lives in `compose.override.yaml` as the `x-tools` anchor, applied
to both `api` and `worker` build args. It used to live in `.env.build`, which a bare
`docker compose build` does not read — so a routine rebuild produced an image with **no
maigret** while still printing "Built", and jobs then failed at run time with "not installed
in this worker image". `.env.build` has been deleted; there is one source of truth, in a
file that cannot be skipped without also dropping proxy mode, and `verify-deployment.sh`
cross-checks `HARVEST_TOOLS` against the binaries actually in the image.

`verify-deployment.sh` asserts the deployed state rather than the intended one: the compose
files in effect, that the live overlay copies still match the tracked ones, that every
published port is bound to the Tailscale address, that the worker is in proxy mode and
cannot reach `1.1.1.1:443` directly, that every allowlisted tool exists in the image, and
that GHunt's credential is present on persistent storage. It prints no secrets. Run it after
every deploy; it exits non-zero if anything disagrees.

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
`fix/auth-db-reconnect` (`daee4b5f`) is pushed to `patched`, so the patches are
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
