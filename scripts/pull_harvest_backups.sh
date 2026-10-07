#!/usr/bin/env bash
set -euo pipefail
umask 077

REMOTE="${HARVEST_BACKUP_REMOTE:-ovh}"
REMOTE_DIR="${HARVEST_BACKUP_REMOTE_DIR:-/opt/harvest/backups}"
LOCAL_DIR="${HARVEST_BACKUP_LOCAL_DIR:-/home/pop/backups/harvest}"
REMOTE_APP_DIR="${HARVEST_REMOTE_APP_DIR:-/opt/harvest/app}"
mkdir -p "$LOCAL_DIR"

rsync -a --ignore-existing   "$REMOTE:$REMOTE_DIR/" "$LOCAL_DIR/"

shopt -s nullglob
for id_file in "$LOCAL_DIR"/*.age.backup-id; do
  archive="${id_file%.backup-id}"
  checksum="$archive.sha256"
  [[ -s "$archive" && -s "$checksum" ]] || continue
  (cd "$LOCAL_DIR" && sha256sum -c "$(basename "$checksum")" >/dev/null)
  id="$(tr -cd '0-9' < "$id_file")"
  [[ -n "$id" ]] || continue
  # Idempotent: setting the same record off-host again only refreshes its transfer time
  # if this script was explicitly rerun after a verified local copy.
  ssh "$REMOTE" "cd '$REMOTE_APP_DIR' && docker compose exec -T api harvest mark-backup-offhost '$id'"
done

python3 "$(dirname "$0")/prune_backups.py" "$LOCAL_DIR"
