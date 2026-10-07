#!/usr/bin/env bash
# Assert the DEPLOYED state, not the intended one.
#
# Every check here exists because something was once silently wrong in a way the deploy
# commands themselves reported as success: `docker compose up -d` printed "Started" with
# the egress overlay missing and the stack in direct mode, and `docker compose build`
# printed "Built" for an image containing no maigret.
#
# Run from the compose project directory, after every deploy:
#
#   cd /opt/harvest/app && ./verify-deployment.sh
#
# Exits non-zero on the first failed assertion. Prints no secrets: tokens, proxy
# credentials and GHunt session material are reported as present/absent only.
set -uo pipefail

PASS=0
FAIL=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$1"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
note() { printf '  --   %s\n' "$1"; }
section() { printf '\n== %s\n' "$1"; }

# Deliberately a bare `docker compose`, with no -f: that is the command an operator types,
# so it is the one that has to be correct. It picks up COMPOSE_FILE from .env.
dc() { docker compose "$@"; }

section "Overlays applied by a bare \`docker compose\` (checked by effect)"
# COMPOSE_FILE lives in .env, which compose reads but this shell does not, so testing
# $COMPOSE_FILE here would report a false failure on a correctly configured host. What
# matters is not the variable but whether each overlay's distinctive effect survives into
# the config compose would actually apply, so assert that instead.
CONFIG=$(dc config --format json 2>/dev/null) || { bad "docker compose config failed"; exit 1; }
marker() { printf '%s' "$CONFIG" | python3 -c '
import json, sys
node, want = json.load(sys.stdin), sys.argv[2]
for key in sys.argv[1].split("."):
    if not isinstance(node, dict) or key not in node:
        print("MISSING")
        raise SystemExit
    node = node[key]
print("PRESENT" if want == "*" or str(node) == want else "GOT:" + str(node))
' "$1" "$2" 2>/dev/null; }

check_marker() { # <description> <dotted.path> <expected|*> <overlay>
    local got; got=$(marker "$2" "$3")
    case "$got" in
        PRESENT) ok "$1 (from $4)" ;;
        MISSING) bad "$1 -- absent, so $4 was NOT applied" ;;
        *)       bad "$1 -- $got, so $4 was not applied as expected" ;;
    esac
}
check_marker "egress-relay service defined"   "services.egress-relay"                        "*"              compose.egress-proxy.yaml
check_marker "worker HARVEST_EGRESS_MODE"     "services.worker.environment.HARVEST_EGRESS_MODE" "proxy"       compose.egress-proxy.yaml
check_marker "worker HOME off tmpfs"          "services.worker.environment.HOME"             "/home/harvest"  compose.ghunt.yaml
check_marker "ghunt home volume declared"     "volumes.harvest-ghunt-home"                   "*"              compose.ghunt.yaml
check_marker "tool pin reaches the build arg" "services.worker.build.args.HARVEST_TOOL_PACKAGES" "*"           compose.override.yaml
check_marker "worker runs 2 threads"          "services.worker.environment.HARVEST_WORKER_THREADS" "2"         compose.override.yaml

# And confirm the mechanism itself, so the NEXT bare command is also correct.
if grep -q '^COMPOSE_FILE=.*compose.egress-proxy.yaml' .env 2>/dev/null &&
   grep -q '^COMPOSE_FILE=.*compose.override.yaml' .env 2>/dev/null &&
   grep -q '^COMPOSE_FILE=.*compose.ghunt.yaml' .env 2>/dev/null && \
   grep -q '^COMPOSE_FILE=.*compose.searxng.yaml' .env 2>/dev/null; then
    ok ".env declares COMPOSE_FILE with every overlay"
else
    bad ".env does not declare COMPOSE_FILE with every overlay -- a bare command would skip some"
fi

section "Live overlay copies match the tracked ones"
# The overlays live at the project root but are tracked under deploy/ovh-vps/. A drifted
# copy means the repo no longer describes what is running.
for f in compose.override.yaml compose.egress-proxy.yaml compose.ghunt.yaml compose.searxng.yaml; do
    if diff -q "$f" "deploy/ovh-vps/$f" >/dev/null 2>&1; then
        ok "$f matches deploy/ovh-vps/$f"
    else
        bad "$f has DRIFTED from deploy/ovh-vps/$f"
    fi
done

# The relay's template and entrypoint are bind-mounted from /opt/harvest/egress-relay, which
# is deliberately outside the repo (it is where the 0600 credential lives). They still have
# to match the tracked originals, or the running relay is not what the repo describes. The
# credential file itself is NOT compared -- it has no tracked counterpart by design.
for f in tinyproxy.conf.template entrypoint.sh; do
    if diff -q "/opt/harvest/egress-relay/$f" "deploy/ovh-vps/egress-relay/$f" >/dev/null 2>&1; then
        ok "egress-relay/$f matches deploy/ovh-vps/egress-relay/$f"
    else
        bad "egress-relay/$f has DRIFTED from deploy/ovh-vps/egress-relay/$f"
    fi
done

# The same for the files the other services bind-mount from outside this checkout.
for pair in "/opt/harvest/spiderfoot-ng/compose.core.yml:spiderfoot-core.yml" \
            "/opt/harvest/sf-dns/Corefile:sf-dns/Corefile" \
            "/opt/harvest/searxng/settings.yml:searxng/settings.yml"; do
    live=${pair%%:*} tracked=deploy/ovh-vps/${pair#*:}
    if diff -q "$live" "$tracked" >/dev/null 2>&1; then
        ok "$live matches $tracked"
    else
        bad "$live has DRIFTED from $tracked (or is missing)"
    fi
done

section "Tailscale-only exposure"
# An interface-bound publish, not a ufw rule, is what keeps this off the public internet:
# docker writes its own DOCKER-USER iptables rules, which bypass ufw entirely.
PORTS=$(printf '%s' "$CONFIG" | python3 -c '
import json, sys
services = json.load(sys.stdin).get("services", {})
for name in sorted(services):
    for port in services[name].get("ports", []):
        print(name, port.get("published", ""), port.get("host_ip", ""))
' 2>/dev/null)
if [[ -z "$PORTS" ]]; then
    ok "no published ports at all"
else
    while read -r svc published host_ip; do
        [[ -z "$svc" ]] && continue
        case "$host_ip" in
            100.118.181.47) ok "$svc publishes $published on the Tailscale address only" ;;
            127.0.0.1)      ok "$svc publishes $published on loopback only" ;;
            *)              bad "$svc publishes $published on '${host_ip:-0.0.0.0}' -- publicly reachable" ;;
        esac
    done <<< "$PORTS"
fi

section "Worker egress posture"
MODE=$(dc exec -T worker printenv HARVEST_EGRESS_MODE 2>/dev/null | tr -d '\r')
PROXY_SET=$(dc exec -T worker sh -c '[ -n "$HARVEST_EGRESS_PROXY" ] && echo yes || echo no' 2>/dev/null | tr -d '\r')
case "$MODE" in
    proxy) ok "HARVEST_EGRESS_MODE=proxy" ;;
    "")    bad "HARVEST_EGRESS_MODE is UNSET in the worker -- the egress overlay was not applied" ;;
    *)     bad "HARVEST_EGRESS_MODE=$MODE (expected proxy)" ;;
esac
[[ "$PROXY_SET" == yes ]] && ok "HARVEST_EGRESS_PROXY is set" || bad "HARVEST_EGRESS_PROXY is empty"

# The structural guarantee: every container that COLLECTS sits on internal networks only,
# so there is no route off-host except the relay (and, for the scanner, the sf-dns resolver).
# A blocked probe is the PASS condition. Proxy env vars are routing, not isolation: the
# scanner and SearXNG both had HTTP(S)_PROXY set and could still connect out directly.
for c in "$(dc ps -q worker 2>/dev/null)" sf-celery-worker harvest-searxng; do
    name=$(docker inspect -f '{{.Name}}' "$c" 2>/dev/null | tr -d /)
    DIRECT=$(docker exec "$c" python3 -c '
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5)
    print("open")
except OSError:
    print("blocked")
' 2>/dev/null | tr -d '\r')
    case "$DIRECT" in
        blocked) ok "direct egress from ${name:-$c} is blocked (no route off-host)" ;;
        open)    bad "direct egress from ${name:-$c} WORKS -- it can bypass the relay" ;;
        *)       bad "could not test direct egress from ${name:-$c}" ;;
    esac
done

section "Upstream proxy credential (never prints the value)"
SECRET=/opt/harvest/egress-relay/upstream.secret
TEMPLATE=/opt/harvest/egress-relay/tinyproxy.conf.template

# The credential must exist outside the repo. /opt/harvest/app IS the git checkout, so a
# secret placed under it could be committed by a careless `git add -A`.
case "$SECRET" in
    /opt/harvest/app/*) bad "the secret path is inside the git repo -- move it out of /opt/harvest/app" ;;
    *) ok "secret lives outside the git checkout" ;;
esac

if [[ -f "$SECRET" ]]; then
    ok "upstream secret file is present"
    PERM=$(stat -c '%a' "$SECRET" 2>/dev/null)
    OWNER=$(stat -c '%U:%G' "$SECRET" 2>/dev/null)
    case "$PERM" in
        600|400) ok "its mode is $PERM, owner $OWNER (not group- or world-readable)" ;;
        *)       bad "its mode is $PERM, owner $OWNER -- must be 600; it was 644 before 2026-10-03, readable by every user on this host" ;;
    esac
else
    bad "upstream secret $SECRET is ABSENT -- the relay will refuse to start (by design: without it, it would egress directly from this VPS)"
fi

# A credential must never reappear in a tracked file. Checked by shape, not by comparing
# against the real value, so this never needs the secret to run.
if [[ -f "$TEMPLATE" ]]; then
    if grep -qE '^[[:space:]]*Upstream[[:space:]]' "$TEMPLATE"; then
        bad "the TRACKED template has an active Upstream line -- credentials belong only in $SECRET"
    else
        ok "tracked template is placeholder-only (no active Upstream line)"
    fi
else
    bad "config template $TEMPLATE is missing"
fi
if git -C /opt/harvest/app grep -qIE '^[[:space:]]*Upstream[[:space:]]+http[[:space:]]+[^[:space:]]+:[^[:space:]]+@' -- deploy 2>/dev/null; then
    bad "a tracked file under deploy/ contains a credentialed Upstream line"
else
    ok "no tracked file under deploy/ carries a credentialed Upstream line"
fi

# THE check that the worker-side probe above cannot make. tinyproxy with no Upstream line
# runs fine and connects straight out from this VPS's address, so proxy-only egress would be
# silently defeated while every other check still passed. Counted, never printed.
if [[ -n "$(docker ps -q -f name=harvest-egress-relay 2>/dev/null)" ]]; then
    UP=$(docker exec harvest-egress-relay sh -c "grep -cE '^Upstream http ' /run/tinyproxy/tinyproxy.conf 2>/dev/null || echo 0" 2>/dev/null | tr -d '\r')
    if [[ "${UP:-0}" -ge 1 ]]; then
        ok "the relay's running config has an Upstream line (assembled at startup, on tmpfs)"
    else
        bad "the relay is running WITHOUT an upstream -- it is egressing directly from this VPS"
    fi
    # The assembled config holds the credential, so it must not be world-readable even
    # inside the container, and it must not have been written to the image's disk layer.
    CPERM=$(docker exec harvest-egress-relay stat -c '%a' /run/tinyproxy/tinyproxy.conf 2>/dev/null | tr -d '\r')
    case "$CPERM" in
        600|400) ok "its assembled config is mode $CPERM inside the container" ;;
        *)       bad "the assembled config is mode ${CPERM:-unknown} inside the container (expected 600)" ;;
    esac
    if docker exec harvest-egress-relay sh -c 'mountpoint -q /run/tinyproxy || grep -q " /run/tinyproxy " /proc/mounts' 2>/dev/null; then
        ok "/run/tinyproxy is a tmpfs (credential never lands on disk)"
    else
        bad "/run/tinyproxy is NOT a tmpfs -- the assembled credential is being written to disk"
    fi
    # The shared outbound limit: the relay enforces it, the worker sizes Maigret's -n from it,
    # so the two must agree. A missing MaxClients line means tinyproxy's compiled-in 100.
    RELAY_MAX=$(docker exec harvest-egress-relay sh -c "sed -n 's/^MaxClients //p' /run/tinyproxy/tinyproxy.conf" 2>/dev/null | tr -d '\r')
    WORKER_MAX=$(dc exec -T worker python -c 'from harvest.config import Settings; print(Settings().egress_max_connections)' 2>/dev/null | tr -d '\r')
    if [[ -n "$RELAY_MAX" && "$RELAY_MAX" == "$WORKER_MAX" ]]; then
        ok "relay MaxClients $RELAY_MAX matches the worker's HARVEST_EGRESS_MAX_CONNECTIONS"
    else
        bad "relay MaxClients '${RELAY_MAX:-unset (tinyproxy default 100)}' != worker HARVEST_EGRESS_MAX_CONNECTIONS '${WORKER_MAX:-unknown}'"
    fi
    # The credential must not be recoverable from container metadata either.
    if docker inspect harvest-egress-relay --format '{{json .Config.Env}} {{json .Config.Cmd}} {{json .Config.Entrypoint}}' 2>/dev/null | grep -qE '[^[:space:]:]+:[^[:space:]:]+@[0-9A-Za-z.-]+:[0-9]+'; then
        bad "a credential-shaped value is exposed in the relay's env/cmd/entrypoint (docker inspect)"
    else
        ok "no credential-shaped value in the relay's env, cmd or entrypoint"
    fi
else
    note "egress-relay is not running; skipping its runtime credential checks"
fi

section "Tools: allowlisted AND actually in the image"
# These must agree. HARVEST_TOOLS naming a binary the image lacks fails every job of that
# kind at run time; an image carrying a binary that is not allowlisted is merely wasted size.
ALLOWED=$(dc exec -T worker printenv HARVEST_TOOLS 2>/dev/null | tr -d '\r')
note "HARVEST_TOOLS=${ALLOWED:-<empty>}"
IFS=',' read -ra TOOLS <<< "$ALLOWED"
for t in "${TOOLS[@]}"; do
    t=$(printf '%s' "$t" | tr -d '[:space:]')
    [[ -z "$t" ]] && continue
    case "$t" in
        spiderfoot)
            # Speaks HTTP, not argv: there is no binary to find.
            if [[ -n "$(dc exec -T worker printenv HARVEST_SPIDERFOOT_URL 2>/dev/null | tr -d '\r')" ]]; then
                ok "spiderfoot allowlisted and HARVEST_SPIDERFOOT_URL is set"
            else
                bad "spiderfoot allowlisted but HARVEST_SPIDERFOOT_URL is empty"
            fi
            ;;
        *)
            if dc exec -T worker sh -c "command -v $t >/dev/null 2>&1"; then
                ok "$t is allowlisted and present in the worker image"
            else
                bad "$t is allowlisted but NOT INSTALLED in the worker image"
            fi
            ;;
    esac
done

section "GHunt credential (presence only, never contents)"
if [[ " ${ALLOWED} " == *ghunt* ]]; then
    GHOME=$(dc exec -T worker printenv HOME 2>/dev/null | tr -d '\r')
    note "worker HOME=$GHOME"
    case "$GHOME" in
        /tmp|/tmp/*) bad "HOME is on tmpfs -- creds.m would not survive a restart (load compose.ghunt.yaml)" ;;
        "")          bad "could not read the worker's HOME" ;;
        *)           ok "HOME is on persistent storage" ;;
    esac
    if dc exec -T worker sh -c 'test -f "$HOME/.malfrats/ghunt/creds.m"' 2>/dev/null; then
        ok "creds.m is present"
    else
        bad "creds.m is ABSENT -- ghunt is allowlisted but every run will be a policy denial"
    fi
    if dc exec -T worker sh -c 'test -w "$HOME/.malfrats/ghunt"' 2>/dev/null; then
        ok "its directory is writable (ghunt rewrites creds.m on session refresh)"
    else
        bad "its directory is not writable -- a session refresh would fail"
    fi

    # ghunt 2.3.4 cannot complete `ghunt email --json` unpatched: parsers/people.py raises
    # KeyError 'container' on any account with a cover photo, and modules/email.py raises
    # NameError on `photos` for every PROFILE container. Both fixes are applied in the
    # Dockerfile -- a TRACKED file -- so a redeploy that checks out a commit predating them
    # reverts both silently, and the Dockerfile's own grep guards cannot catch that because
    # removing the patch removes the guard with it. This asserts the DEPLOYED IMAGE instead
    # of the build that produced it, which is the only check that survives that mistake.
    P=/opt/uv-tools/ghunt/lib/python3.12/site-packages/ghunt/parsers/people.py
    E=/opt/uv-tools/ghunt/lib/python3.12/site-packages/ghunt/modules/email.py
    if dc exec -T worker sh -c "grep -q containerType $P && ! grep -q '\"photos\": photos,' $E" 2>/dev/null; then
        ok "ghunt crash patches are in the running image (people.py + email.py)"
    else
        bad "ghunt in the running image is UNPATCHED -- \`ghunt email --json\` will fail on the first target; re-apply the Dockerfile patch and rebuild"
    fi
else
    note "ghunt is not allowlisted; skipping"
fi

section "Discovery search (SearXNG)"
SEARCH_URL=$(grep -E '^HARVEST_SEARCH_URL=' .env 2>/dev/null | cut -d= -f2-)
if [[ -z "$SEARCH_URL" ]]; then
    bad "HARVEST_SEARCH_URL is unset -- discovery from an objective is unavailable, and a job with seeds skips search SILENTLY rather than failing"
else
    ok "HARVEST_SEARCH_URL is set"
    # It must be reachable from the WORKER, which is the confined container. Reachable from
    # the host proves nothing: the worker has no gateway and resolves only harvest-egress.
    if dc exec -T worker python -c "
import urllib.request,sys
urllib.request.urlopen('$SEARCH_URL', timeout=15)
" >/dev/null 2>&1; then
        ok "the worker can reach it on the internal network"
    else
        bad "the worker CANNOT reach $SEARCH_URL -- attach the search service to harvest-egress"
    fi
    # The format matters more than reachability: SearXNG omits json from `formats` by
    # default and then answers 403 to exactly the request Harvest makes, while its HTML UI
    # keeps working perfectly -- so this fails as "search returned HTTP 403", which reads
    # like a broken deployment rather than one missing config line.
    # One real search, here rather than in the container healthcheck: it is outbound traffic
    # through every provider, so it runs once per deploy, not every minute.
    DOWN=$(dc exec -T worker python -c "
import json,urllib.request
d=json.load(urllib.request.urlopen('$SEARCH_URL/search?q=ping&format=json', timeout=25))
assert 'results' in d
print(','.join(sorted(str(e[0]) for e in d.get('unresponsive_engines') or [])) or 'none')
" 2>/dev/null | tr -d '\r')
    if [[ -n "$DOWN" ]]; then
        ok "it serves format=json (the only format Harvest uses)"
        [[ "$DOWN" == none ]] && ok "every engine answered a test search" \
            || note "engines down for a test search right now: $DOWN (provider health, not config)"
    else
        bad "it does not serve format=json -- add 'json' to search.formats in settings.yml"
    fi
    # The EFFECTIVE engine set, not the overlay: use_default_settings merges the image's
    # defaults, and an engine the image marks inactive never runs whatever the overlay says.
    ENGINES=$(dc exec -T worker python -c "
import json,urllib.request
d=json.load(urllib.request.urlopen('$SEARCH_URL/config', timeout=15))
print(','.join(sorted(e['name'] for e in d['engines'] if e.get('enabled'))))
" 2>/dev/null | tr -d '\r')
    if [[ "$ENGINES" == "bing,brave,duckduckgo" ]]; then
        ok "its enabled engines are exactly bing, brave, duckduckgo"
    else
        bad "its enabled engines are '${ENGINES:-unreadable}', not bing,brave,duckduckgo -- check use_default_settings.engines.keep_only"
    fi
    # Its own engine queries must leave through the relay, or the searches are attributable
    # to this VPS while every other request in the deployment is not.
    if [[ -n "$(docker ps -q -f name=harvest-searxng 2>/dev/null)" ]]; then
        if docker exec harvest-searxng grep -q 'egress-relay' /etc/searxng/settings.yml 2>/dev/null; then
            ok "its outgoing.proxies points at the egress relay"
        else
            bad "harvest-searxng has no egress-relay proxy configured -- its engine queries would leave directly"
        fi
        if docker port harvest-searxng 2>/dev/null | grep -q .; then
            bad "harvest-searxng PUBLISHES a port -- it must stay private (a reachable SearXNG is an open search relay)"
        else
            ok "it publishes no port (private service)"
        fi
    fi
fi

section "SpiderFoot scan egress"
SFE=$(grep -E '^HARVEST_SPIDERFOOT_EGRESS=' .env 2>/dev/null | cut -d= -f2-)
if [[ " ${ALLOWED} " != *spiderfoot* ]]; then
    note "spiderfoot is not allowlisted; skipping"
elif [[ "$MODE" != proxy ]]; then
    note "not in proxy mode; spiderfoot egress is unconstrained by design"
else
    # The declaration Harvest gates the tool on. Harvest cannot observe another container's
    # egress, so it reads this; the checks below are what the declaration stands for.
    if [[ "$SFE" == "proxy-env" ]]; then
        ok "HARVEST_SPIDERFOOT_EGRESS=proxy-env (Harvest will run the tool)"
    else
        bad "HARVEST_SPIDERFOOT_EGRESS=${SFE:-unset} -- in proxy mode Harvest refuses every spiderfoot run (this is why the tool silently never ran)"
    fi
    if [[ -n "$(docker ps -q -f name=sf-celery-worker 2>/dev/null)" ]]; then
        # The scanner, not sf-api, is what runs modules and makes the requests.
        if docker exec sf-celery-worker printenv HTTPS_PROXY 2>/dev/null | grep -q 'egress-relay'; then
            ok "the scanner container carries HTTPS_PROXY pointing at the relay"
        else
            bad "sf-celery-worker has no HTTPS_PROXY to the relay -- its modules egress DIRECTLY from this host"
        fi
        # A long scan leaks the celery child's Postgres pool; the next scan in that child
        # then dies before starting (2026-10-04). One scan per child releases it.
        if [[ "$(docker exec sf-celery-worker printenv SF_CELERY_MAX_TASKS_PER_CHILD 2>/dev/null)" == "1" ]]; then
            ok "the scanner recycles its worker process after every scan"
        else
            bad "sf-celery-worker lacks SF_CELERY_MAX_TASKS_PER_CHILD=1 -- a leaked DB pool can kill the next scan before it starts"
        fi
        if docker inspect sf-celery-worker --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null | grep -q harvest-egress; then
            ok "the scanner is attached to harvest-egress (so the relay resolves)"
        else
            bad "sf-celery-worker is NOT on harvest-egress -- egress-relay will not resolve and every module request fails; run: docker network connect harvest-egress sf-celery-worker"
        fi
        # DNS is the one non-HTTP path the scanner keeps: through sf-dns, nowhere else.
        if docker exec sf-celery-worker python3 -c "import socket; socket.gethostbyname('example.com')" >/dev/null 2>&1 &&
           docker logs --since 2m sf-dns 2>&1 | grep -q 'example.com'; then
            ok "the scanner resolves names through sf-dns (and the query is in its log)"
        else
            bad "the scanner cannot resolve through sf-dns -- every DNS module and resolve_host() fails"
        fi
        # What each allowlisted module needs from the network. Confined, the scanner has HTTP
        # (relay) and DNS (sf-dns) only, so a module needing anything else would fail on every
        # scan without saying why: it has to be allowlisted knowingly, not discovered later.
        SF_MODULES=$(grep -E '^HARVEST_SPIDERFOOT_MODULES=' .env 2>/dev/null | cut -d= -f2- | tr ',' ' ')
        for m in $SF_MODULES; do
            src=$(docker exec sf-celery-worker cat "/home/spiderfoot/modules/$m.py" 2>/dev/null)
            [[ -z "$src" ]] && { bad "$m is allowlisted but not in the scanner image"; continue; }
            t=()
            grep -qE 'fetch_url|fetchUrl' <<< "$src" && t+=(http)
            grep -qE 'resolve_host|reverse_resolve|resolveHost|resolveIP|dns\.resolver' <<< "$src" && t+=(dns)
            if grep -qE 'socket\.socket|create_connection|smtplib|subprocess|whois\.whois|telnetlib' <<< "$src"; then
                bad "$m uses a transport other than HTTP/DNS -- it cannot work from the confined scanner"
            else
                note "$m: ${t[*]:-no network}"
            fi
        done
        # The measurement, not the declaration: where the scanner's packets actually exit.
        # SpiderFoot's own _socks* config is deliberately not consulted -- it is never
        # reloaded at startup and its scanner ignores it (measured: 0 bytes via the relay).
        SF_IP=$(docker exec sf-celery-worker python -c "
import requests
print(requests.get('https://api.ipify.org', timeout=25).text.strip())
" 2>/dev/null | tr -d '\r')
        HOST_IP=$(curl -s --max-time 15 https://api.ipify.org 2>/dev/null)
        if [[ -z "$SF_IP" ]]; then
            bad "could not read the scanner's exit IP"
        elif [[ "$SF_IP" == "$HOST_IP" ]]; then
            bad "the scanner's exit IP equals this host's ($HOST_IP) -- its scans are attributable to this VPS"
        else
            ok "the scanner's exit IP is the upstream proxy's, not this host's"
        fi
    fi
fi

section "Service health"
dc ps --format '{{.Service}} {{.Status}}' 2>/dev/null | while read -r svc status; do
    [[ -z "$svc" ]] && continue
    case "$status" in
        Up*) printf '  ok   %s: %s\n' "$svc" "$status" ;;
        *)   printf '  FAIL %s: %s\n' "$svc" "$status" ;;
    esac
done
# The loop above runs in a subshell, so re-check the failure condition here.
if dc ps --format '{{.Status}}' 2>/dev/null | grep -qv '^Up'; then
    bad "a service is not Up"
else
    ok "all services Up"
fi

printf '\n== %d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]] || exit 1
