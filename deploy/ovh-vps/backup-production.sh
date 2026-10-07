#!/usr/bin/env bash
set -euo pipefail
umask 077

APP_DIR="${HARVEST_APP_DIR:-/opt/harvest/app}"
BACKUP_DIR="${HARVEST_BACKUP_DIR:-/opt/harvest/backups}"
AGE_RECIPIENT="${HARVEST_BACKUP_AGE_RECIPIENT:?set HARVEST_BACKUP_AGE_RECIPIENT to the workstation backup recipient}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/harvest-$STAMP.tar.gz.age"
TMP="$(mktemp -d /tmp/harvest-backup.XXXXXX)"
DB_TMP="/data/.harvest-backup-$STAMP.sqlite"

cleanup() {
  if [[ -n "${API_CONTAINER:-}" ]]; then
    docker exec "$API_CONTAINER" rm -f "$DB_TMP" >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

command -v age >/dev/null || { echo "age is required" >&2; exit 2; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
mkdir -p "$BACKUP_DIR"
cd "$APP_DIR"
API_CONTAINER="$(docker compose ps -q api)"
[[ -n "$API_CONTAINER" ]] || { echo "Harvest api container not found" >&2; exit 2; }

# SQLite online backup through the application, never a live-file copy.
docker compose exec -T api harvest backup "$DB_TMP"
docker cp "$API_CONTAINER:$DB_TMP" "$TMP/harvest.sqlite"
docker exec "$API_CONTAINER" rm -f "$DB_TMP"

# SpiderFoot PostgreSQL dump. Resolve by Compose labels rather than a fragile container name.
PG_CONTAINER="$(docker ps -q   --filter label=com.docker.compose.project=spiderfoot-ng   --filter label=com.docker.compose.service=postgres | head -1)"
[[ -n "$PG_CONTAINER" ]] || { echo "SpiderFoot postgres container not found" >&2; exit 2; }
docker exec "$PG_CONTAINER" sh -lc   'exec pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$TMP/spiderfoot.sql"
[[ -s "$TMP/spiderfoot.sql" ]] || { echo "empty SpiderFoot dump" >&2; exit 2; }

mkdir -p "$TMP/config"
for path in   "$APP_DIR/.env"   /opt/harvest/spiderfoot-ng/.env   /opt/harvest/egress-relay/upstream.secret   /opt/harvest/searxng/settings.yml   /opt/harvest/sf-dns/Corefile
do
  if [[ -f "$path" ]]; then
    # Flatten names but retain a map so restore never guesses the original path.
    key="$(printf '%s' "$path" | sha256sum | cut -c1-12)"
    cp --preserve=mode,timestamps "$path" "$TMP/config/$key"
    printf '%s\t%s\n' "$key" "$path" >> "$TMP/config-paths.tsv"
  fi
done

{
  printf 'created_utc=%s\n' "$STAMP"
  printf 'harvest_commit=%s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  printf 'api_image=%s\n' "$(docker inspect -f '{{.Image}}' "$API_CONTAINER")"
  printf 'postgres_image=%s\n' "$(docker inspect -f '{{.Image}}' "$PG_CONTAINER")"
} > "$TMP/release.txt"

(
  cd "$TMP"
  find . -type f ! -name manifest.sha256 -print0 |
    sort -z |
    xargs -0 sha256sum > manifest.sha256
)
MANIFEST_HASH="$(sha256sum "$TMP/manifest.sha256" | awk '{print $1}')"

tar -C "$TMP" -czf - . | age -r "$AGE_RECIPIENT" -o "$OUT.tmp"
test -s "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
sha256sum "$OUT" > "$OUT.sha256"

BACKUP_ID="$(docker compose exec -T api harvest record-backup   --kind production --manifest-hash "$MANIFEST_HASH" --verified | tr -d '\r')"
printf '%s\n' "$BACKUP_ID" > "$OUT.backup-id"

echo "$OUT"
