#!/usr/bin/env bash
# Nightly pg_dump of the production database, 14-day retention
# (SPEC Section 10). Run by kestrel-backup.timer; restore: docs/runbook.md.
#
# Dumps from inside the postgres container (it publishes no host port),
# custom format (-Fc: compressed, restorable table by table with pg_restore).
set -euo pipefail

REPO_DIR="${KESTREL_REPO_DIR:-/root/trading_bot/kestrel}"
BACKUP_DIR="${KESTREL_BACKUP_DIR:-/var/backups/kestrel}"
RETENTION_DAYS="${KESTREL_BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/kestrel-$stamp.dump"

docker compose -f "$REPO_DIR/deploy/docker-compose.prod.yml" exec -T postgres \
  pg_dump -U kestrel -d kestrel -Fc > "$out.partial"
mv "$out.partial" "$out"   # never leave a half-written file looking like a good backup
chmod 600 "$out"

# A dump that pg_restore can't read is not a backup.
docker compose -f "$REPO_DIR/deploy/docker-compose.prod.yml" exec -T postgres \
  pg_restore --list < "$out" > /dev/null

find "$BACKUP_DIR" -name 'kestrel-*.dump' -mtime +"$RETENTION_DAYS" -delete
echo "backup ok: $out ($(du -h "$out" | cut -f1))"
