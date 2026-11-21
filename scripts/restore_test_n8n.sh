#!/usr/bin/env bash
# Restore TEST: loads the dump from a backup archive into a scratch database (n8n_restore_test) next to the live
# one, checks it, and drops it. The live database is never touched. Prints "RESTORE_OK tables=N" on success.
# A real restore (stop n8n, pg_restore into an empty database, start n8n) is documented in README.md.
# Usage: restore_test_n8n.sh /opt/n8n/backups/n8n-YYYYmmdd-HHMMSS.tar.gz
set -euo pipefail
umask 077

ARCHIVE="${1:?usage: restore_test_n8n.sh <archive>}"
PG="n8n-postgres"
SCRATCH="n8n_restore_test"
[ -f "$ARCHIVE" ] || { echo "no such archive" >&2; exit 2; }

tmp="$(mktemp -d)"
cleanup() {
  docker exec "$PG" psql -U n8n -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH" > /dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

tar -xzf "$ARCHIVE" -C "$tmp" ./n8n.dump
docker exec "$PG" psql -U n8n -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH" > /dev/null
docker exec "$PG" psql -U n8n -d postgres -qc "CREATE DATABASE $SCRATCH" > /dev/null
docker exec -i "$PG" pg_restore -U n8n -d "$SCRATCH" --no-owner < "$tmp/n8n.dump"

tables="$(docker exec "$PG" psql -U n8n -d "$SCRATCH" -Atc "select count(*) from information_schema.tables where table_schema='public'")"
has_wf="$(docker exec "$PG" psql -U n8n -d "$SCRATCH" -Atc "select to_regclass('public.workflow_entity') is not null")"
[ "$has_wf" = "t" ] && [ "$tables" -gt 20 ] || { echo "restored copy looks wrong (tables=$tables)" >&2; exit 4; }
echo "RESTORE_OK tables=$tables"
