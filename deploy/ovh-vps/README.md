# ovh-vps deployment

The live deployment's configuration, kept here so it is reproducible rather than
existing only on the host. These files are **not** loaded by a plain
`docker compose up` in the repository root; they are copies of what runs at
`/opt/harvest` on the VPS.

| File | Lives at | Purpose |
| --- | --- | --- |
| `compose.override.yaml` | `/opt/harvest/app/` | Tailscale-only port binding, FlareSolverr, shared `osint-net` |
| `compose.egress-proxy.yaml` | `/opt/harvest/app/` | **Opt-in** proxy-only egress (not loaded by default) |
| `egress-relay/tinyproxy.conf` | `/opt/harvest/egress-relay/` | Egress relay; set `Upstream` to the ISP proxy |
| `spiderfoot-core.yml` | `/opt/harvest/spiderfoot-ng/compose.core.yml` | SpiderFoot NG minimal core + healthcheck fixes |

Every published port is bound to the Tailscale address rather than `0.0.0.0`.
Docker writes its own `DOCKER-USER` iptables rules that bypass ufw, so the
interface-bound publish — not a ufw rule — is what keeps these services off the
public internet.

## Normal (direct egress) startup

```sh
cd /opt/harvest/app
set -a; . .env.build; set +a          # HARVEST_TOOL_PACKAGES for the build
docker compose -f compose.yaml -f compose.override.yaml up -d
```

## Proxy-only egress

Requires the internal network to exist first. It is created once, outside both
compose projects, so neither stack depends on the other's startup order:

```sh
docker network create --internal harvest-egress
```

Then set the `Upstream` line in `egress-relay/tinyproxy.conf` and:

```sh
docker compose -f compose.yaml -f compose.override.yaml \
               -f compose.egress-proxy.yaml up -d
```

The worker then joins only `harvest-egress`, which has no gateway, so it has no
route to the internet at all — the relay is the only way out. A forgotten
firewall rule silently restores direct egress; a missing route cannot. Harvest
additionally probes `HARVEST_EGRESS_PROBE` and refuses to run Maigret if direct
egress turns out to work.

See `/opt/harvest/DEPLOYMENT.md` for which traffic paths this does and does not
cover — notably DNS, which an HTTP proxy cannot carry.
