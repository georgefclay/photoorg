#!/usr/bin/env bash
# ~/photoorg/ops/pg_dump_photoorg.sh (chmod 700)
#
# Nightly Postgres dump of the photoorg DB. Managed by cron; do not run
# manually except for testing. Password lives in web/.env so we don't
# duplicate secrets. Keeps 14 days.
set -euo pipefail
umask 077
BACKUP_DIR="$HOME/backups"
mkdir -p "$BACKUP_DIR"
DATE=$(date +%F)
DBPASS=$(grep -E "^DATABASE_URL=" "$HOME/photoorg/web/.env" | sed 's|.*://photoorg:||' | sed 's|@.*||')
PGPASSWORD="$DBPASS" pg_dump -U photoorg -h localhost -Fc photoorg \
  > "$BACKUP_DIR/photoorg-${DATE}.dump"
gzip -f "$BACKUP_DIR/photoorg-${DATE}.dump"
# 14-day rotation.
find "$BACKUP_DIR" -maxdepth 1 -type f -name "photoorg-*.dump.gz" -mtime +14 -delete
