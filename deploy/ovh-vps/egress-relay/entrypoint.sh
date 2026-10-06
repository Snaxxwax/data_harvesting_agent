#!/bin/sh
# Assemble tinyproxy's config at container start, so the upstream proxy credential never has
# to exist in a tracked file, a command argument, or the container's environment.
#
# Why each of those three is avoided rather than just the tracked file:
#
#   argv        -- a value passed as an argument is readable by any process on the host for
#                  the life of the process via /proc/<pid>/cmdline, and shows up in `ps`.
#                  So the credential is never an argument to anything: it is read with the
#                  `read` builtin (no process spawned, nothing in argv) and written with a
#                  heredoc (which never reaches argv at all). In particular NOT with
#                  `sed "s/X/$PASS/"`, which would publish it to every user on the box.
#   environment -- an env var is visible in `docker inspect`, in `docker compose config`,
#                  and in /proc/1/environ inside the container, so passing it through compose
#                  would trade a 0644 host file for an equally readable container attribute.
#                  The credential arrives as a bind-mounted 0600 file instead.
#   logs        -- nothing here echoes the value, and every failure message below names only
#                  the path or the missing key. tinyproxy at LogLevel Info does not log the
#                  upstream credential either.
#
# Fail-closed is the whole point. tinyproxy with no `Upstream` line does not error -- it
# runs and connects to destinations DIRECTLY from this host's address, which silently
# defeats proxy-only egress while the worker still believes it is proxied and Harvest's own
# egress probe still passes (that probe proves the WORKER has no route, not that the RELAY
# uses an upstream). So a missing or empty credential must stop the container, not degrade it.
set -eu

TEMPLATE=${TINYPROXY_TEMPLATE:-/etc/tinyproxy/tinyproxy.conf.template}
SECRET=${TINYPROXY_UPSTREAM_FILE:-/run/secrets/egress-upstream}
CONF=${TINYPROXY_CONF:-/run/tinyproxy/tinyproxy.conf}
KEY=HARVEST_EGRESS_UPSTREAM
# Not a secret, so it is an ordinary env var, set once in .env for the relay, worker and api.
MAX_CONNECTIONS=${HARVEST_EGRESS_MAX_CONNECTIONS:-128}

die() { echo "egress-relay: $1" >&2; exit 1; }

[ -f "$TEMPLATE" ] && [ -r "$TEMPLATE" ] || die "config template is missing or unreadable at $TEMPLATE"

# `-d` before `-f`, because this is the case that actually happens: when the host path for a
# bind mount does not exist, Docker CREATES A DIRECTORY there, and the container then sees a
# perfectly readable directory rather than a missing file. Without this branch the failure
# still fails closed, but it reports "absent or empty", which sends whoever is debugging a
# downed relay looking inside the file instead of at the host path that was never created.
[ -d "$SECRET" ] && die "$SECRET is a DIRECTORY, not a file -- Docker creates one when the host bind-mount source is missing; create /opt/harvest/egress-relay/upstream.secret (0600) on the host and recreate this container"
[ -f "$SECRET" ] && [ -r "$SECRET" ] || die "upstream credential file is missing or unreadable at $SECRET -- refusing to start, because without it this relay would egress directly from this host"

# Read with the `read` builtin only. `$KEY=` prefix match rather than sourcing the file, so a
# stray line in the secret cannot execute as shell. The `|| [ -n "$line" ]` tail makes a file
# with no trailing newline still yield its last line.
UPSTREAM=''
while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
        "$KEY"=*) UPSTREAM=${line#"$KEY"=} ;;
        *) continue ;;
    esac
done <"$SECRET"

# A CR from a CRLF-saved file would be appended into the config and make the upstream
# unresolvable, so strip it rather than fail obscurely later. Parameter expansion, not
# `printf "$UPSTREAM" | tr -d '\r'`: only the CR itself goes through argv here, never the
# credential -- the same rule as everywhere else in this script.
CR=$(printf '\r')
UPSTREAM=${UPSTREAM%"$CR"}

[ -n "$UPSTREAM" ] || die "$KEY is absent or empty in $SECRET -- refusing to start rather than egress directly"
case "$UPSTREAM" in
    *@*:*) : ;;
    *) die "$KEY is not in user:password@host:port form (value not shown)" ;;
esac

# The deployment-wide outbound connection limit. Without a MaxClients line tinyproxy silently
# uses its compiled-in 100, which is how this relay ran before the limit was configurable, so
# a bad value stops the relay instead of reverting to an unconfigured limit nobody chose.
case "$MAX_CONNECTIONS" in
    '' | *[!0-9]*) die "HARVEST_EGRESS_MAX_CONNECTIONS must be a whole number, got '$MAX_CONNECTIONS'" ;;
esac
[ "$MAX_CONNECTIONS" -ge 1 ] && [ "$MAX_CONNECTIONS" -le 500 ] ||
    die "HARVEST_EGRESS_MAX_CONNECTIONS must be between 1 and 500, got $MAX_CONNECTIONS"

mkdir -p "$(dirname "$CONF")"
# Create empty and tighten the mode BEFORE any content lands, so the finished file is never
# briefly world-readable. /run/tinyproxy is a tmpfs, so the assembled config -- the only
# place the credential exists at rest in this container -- never touches disk.
: >"$CONF"
chmod 600 "$CONF"
cat "$TEMPLATE" >>"$CONF"
cat >>"$CONF" <<EOF

# Appended at startup by entrypoint.sh from HARVEST_EGRESS_MAX_CONNECTIONS. Clients beyond it
# wait in the listen backlog until a slot frees; they are queued, not refused.
MaxClients ${MAX_CONNECTIONS}

# Appended at startup by entrypoint.sh from $SECRET. Never written to a tracked file.
Upstream http ${UPSTREAM}
EOF

# tinyproxy parses the config as root, then drops to the User/Group in the template.
exec tinyproxy -d -c "$CONF"
