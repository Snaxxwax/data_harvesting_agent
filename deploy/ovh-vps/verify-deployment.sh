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

section "Compose files in effect (bare \`docker compose\`, no -f)"
# `config --format json` resolves everything compose would actually apply.
CONFIG=$(dc config --format json 2>/dev/null) || { bad "docker compose config failed"; exit 1; }
for f in compose.yaml compose.override.yaml compose.egress-proxy.yaml compose.ghunt.yaml; do
    if [[ ":${COMPOSE_FILE:-}:" == *":$f:"* ]]; then
        ok "$f is named in COMPOSE_FILE"
    else
        bad "$f is NOT in COMPOSE_FILE -- a bare compose command would skip it"
    fi
done

section "Live overlay copies match the tracked ones"
# The overlays live at the project root but are tracked under deploy/ovh-vps/. A drifted
# copy means the repo no longer describes what is running.
for f in compose.override.yaml compose.egress-proxy.yaml compose.ghunt.yaml; do
    if diff -q "$f" "deploy/ovh-vps/$f" >/dev/null 2>&1; then
        ok "$f matches deploy/ovh-vps/$f"
    else
        bad "$f has DRIFTED from deploy/ovh-vps/$f"
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

# The structural guarantee: the worker is on an internal network with no gateway, so there
# is no route off-host except the relay. A blocked probe is the PASS condition. This is
# what the application's own pre-run probe checks too, so a failure here means tool runs
# would be refused rather than leaking -- but it still means the posture is broken.
DIRECT=$(dc exec -T worker python -c '
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5)
    print("open")
except OSError:
    print("blocked")
' 2>/dev/null | tr -d '\r')
case "$DIRECT" in
    blocked) ok "direct egress from the worker is blocked (no route off-host)" ;;
    open)    bad "direct egress from the worker WORKS -- proxy-only mode is a fiction" ;;
    *)       bad "could not test direct egress from the worker" ;;
esac

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
else
    note "ghunt is not allowlisted; skipping"
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
