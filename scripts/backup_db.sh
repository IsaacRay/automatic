#!/usr/bin/env bash
#
# Best-effort logical backup of the adhdbot Postgres database.
#
# Wired as the first ExecStop step of adhdbot.service (see
# deploy/adhdbot.service.d/backup.conf) so a snapshot is taken on every
# `systemctl restart`/`stop` — while the db container is still up — before
# the stack is brought down and recreated. Also runnable by hand anytime.
#
# Writes timestamped gzipped dumps to $ADHDBOT_BACKUP_DIR (default
# ./backups) and prunes to the newest $ADHDBOT_BACKUP_KEEP (default 30).
# Always exits 0 so a backup failure never blocks the bot from restarting.

set -uo pipefail

COMPOSE_DIR="${ADHDBOT_DIR:-/home/iray/automatic}"
BACKUP_DIR="${ADHDBOT_BACKUP_DIR:-$COMPOSE_DIR/backups}"
KEEP="${ADHDBOT_BACKUP_KEEP:-30}"
DOCKER="${ADHDBOT_DOCKER:-/snap/bin/docker}"
DB_USER="${ADHDBOT_DB_USER:-adhdbot}"
DB_NAME="${ADHDBOT_DB_NAME:-adhdbot}"

log() { echo "adhdbot-backup: $*" >&2; }

mkdir -p "$BACKUP_DIR" || { log "cannot create $BACKUP_DIR"; exit 0; }
cd "$COMPOSE_DIR" || { log "cannot cd to $COMPOSE_DIR"; exit 0; }

# Resolve the db container and confirm it's actually running; skip cleanly
# otherwise (e.g. a cold start where there's nothing to back up yet).
cid="$("$DOCKER" compose ps -q db 2>/dev/null)"
if [ -z "$cid" ] || [ -z "$("$DOCKER" ps -q --no-trunc --filter "id=$cid" 2>/dev/null)" ]; then
  log "db container not running — nothing to back up, skipping"
  exit 0
fi

ts="$(date +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/adhdbot-$ts.sql.gz"
tmp="$out.partial"

if "$DOCKER" exec "$cid" pg_dump -U "$DB_USER" "$DB_NAME" 2>/dev/null | gzip -c > "$tmp"; then
  # Guard against a silently-empty dump (pg_dump can fail mid-pipe).
  if [ -s "$tmp" ] && gzip -t "$tmp" 2>/dev/null; then
    mv "$tmp" "$out"
    log "wrote $out ($(du -h "$out" | cut -f1))"
  else
    rm -f "$tmp"
    log "dump was empty/corrupt — discarded"
    exit 0
  fi
else
  rm -f "$tmp"
  log "pg_dump failed"
  exit 0
fi

# Prune: keep the newest $KEEP dumps, delete the rest.
ls -1t "$BACKUP_DIR"/adhdbot-*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f

exit 0
