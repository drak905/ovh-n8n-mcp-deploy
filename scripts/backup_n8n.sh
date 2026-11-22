#!/usr/bin/env bash
# Online backup of n8n: a Postgres dump (custom format, checked with pg_restore -l) plus the n8n data folder and the files folder.
# Runs as root (cron.d) because data/ belongs to uid 1000, mode 700.
# The encryption key is NOT in the archive: the env file is never read, and n8n's own `config` file (which can hold a
# copy of the key) is left out. Without the key, saved credentials in the dump cannot be decrypted, so keep the
# key in your password manager.
# Backups stay on the same disk. Copy them off the VPS if you need disaster recovery.
set -euo pipefail
umask 077

DATA_DIR="${N8N_DATA_DIR:-/opt/n8n/data}"
N8N_FILES_DIR="${N8N_FILES_DIR:-/opt/n8n/files}"   # the one folder the n8n file nodes may use
OUT_DIR="${N8N_BACKUP_DIR:-/opt/n8n/backups}"
KEEP_DAYS="${N8N_KEEP_DAYS:-14}"
PG="n8n-postgres"

mkdir -p "$OUT_DIR"
exec 9>"$OUT_DIR/.backup.lock"
flock -n 9 || { echo "backup already running" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

docker exec "$PG" pg_dump -U n8n -d n8n -Fc > "$tmp/n8n.dump"
docker exec -i "$PG" pg_restore -l < "$tmp/n8n.dump" > /dev/null || { echo "pg_restore -l failed" >&2; exit 3; }
[ "$(head -c 5 "$tmp/n8n.dump")" = "PGDMP" ] || { echo "dump header wrong" >&2; exit 3; }

mkdir "$tmp/data"
tar -C "$DATA_DIR" --exclude=./config -cf - . | tar -C "$tmp/data" -xf -
mkdir "$tmp/files"
tar -C "$N8N_FILES_DIR" -cf - . | tar -C "$tmp/files" -xf -

stamp="$(date +%Y%m%d-%H%M%S)"
final="$OUT_DIR/n8n-$stamp.tar.gz"
tar -czf "$final.partial" -C "$tmp" .
tar -tzf "$final.partial" > /dev/null
mv "$final.partial" "$final"
chmod 600 "$final"

find "$OUT_DIR" -maxdepth 1 -type f -name 'n8n-*.tar.gz' -mtime +"$KEEP_DAYS" -delete
echo "$final"
