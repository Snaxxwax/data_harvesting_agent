#!/usr/bin/env bash
# Interactive `ghunt login` inside the worker. Run this on the host, by hand, in your own
# terminal -- never through automation, and never piped anywhere.
#
#   cd /opt/harvest/app && ./ghunt-login.sh
#
# WHAT THIS PRINTS
#
# GHunt echoes the OAuth2 token it receives, then the connected account's name and email,
# then saves a master token. The token lines are SECRETS: a master token is equivalent to a
# logged-in browser for that account, is not scoped, and can only be revoked by changing the
# account's password or signing out all its sessions. Do not copy this terminal's output
# into a chat, a ticket, a log or a commit.
#
# WHY THE WRAPPER
#
# Two things are easy to get wrong and both fail confusingly:
#
# 1. The proxy. GHunt's get_httpx_client() builds its client with no proxy argument and
#    leaves trust_env at its default, so HTTP(S)_PROXY is the only route it reads. The
#    worker container does not set those -- harvest injects them only into the tool
#    subprocesses it spawns -- and the worker sits on an internal Docker network with no
#    gateway. Without them the login cannot reach Google at all, and the error looks like a
#    bad token rather than no route.
# 2. A TTY. `exec -T` would disable it and the prompts would not work.
#
# WHICH LOGIN METHOD
#
# GHunt offers four. Option [1] (Companion listening mode) states it is "currently not
# compatible with docker" -- it binds a local port in the browser's machine, not here.
# Use one of:
#   [2] paste the base64 blob from the GHunt Companion browser extension
#   [3] paste an oauth_token  (starts with "oauth2_4/")
#   [4] paste a master token  (starts with "aas_et/")
#
# Use a DEDICATED Google account. Every `ghunt email` lookup is made as that account, so
# Google associates the queries with it and it becomes attributable to this deployment's
# traffic, with the usual risk of rate limiting or suspension.
set -euo pipefail

PROXY=$(docker compose exec -T worker printenv HARVEST_EGRESS_PROXY 2>/dev/null | tr -d '\r')
if [[ -z "$PROXY" ]]; then
    echo "HARVEST_EGRESS_PROXY is empty in the worker." >&2
    echo "Either the egress overlay is not applied (run ./verify-deployment.sh), or this" >&2
    echo "deployment is in direct mode -- in which case drop the -e flags below." >&2
    exit 1
fi

echo "Routing the login through the configured relay. Nothing below is captured or logged."
echo
exec docker compose exec \
    -e "HTTP_PROXY=$PROXY" -e "HTTPS_PROXY=$PROXY" \
    -e "http_proxy=$PROXY" -e "https_proxy=$PROXY" \
    worker ghunt login
