#!/usr/bin/env bash
# Restore the hermes-home database from a backup directory.
#
# Refuses to run while the service is up: restoring underneath a live process
# leaves it holding a stale WAL and is a good way to corrupt both copies.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-}"
DB="${DB:-$ROOT/data/hermes-home.db}"

if [ -z "$SRC" ]; then
    echo "usage: $0 <backup-directory>" >&2
    echo "available:" >&2
    ls -1dt "$HOME"/hermes-home-backups/*/ 2>/dev/null | head -10 | sed 's/^/  /' >&2
    exit 1
fi

BACKUP_DB="$SRC/hermes-home.db"
[ -f "$BACKUP_DB" ] || { echo "no hermes-home.db in $SRC" >&2; exit 1; }

if docker compose -f "$ROOT/docker-compose.yml" ps --status running 2>/dev/null | grep -q hermes-home; then
    echo "hermes-home is running. Stop it first:" >&2
    echo "    make stop" >&2
    exit 1
fi

INTEGRITY="$(sqlite3 "$BACKUP_DB" 'PRAGMA integrity_check;')"
[ "$INTEGRITY" = "ok" ] || { echo "backup fails integrity check: $INTEGRITY" >&2; exit 1; }

# Never overwrite the current database without keeping it. If the restore turns
# out to be the wrong snapshot, the state you replaced is still there.
if [ -f "$DB" ]; then
    ASIDE="$DB.replaced-$(date -u +%Y%m%d-%H%M%S)"
    mv "$DB" "$ASIDE"
    rm -f "$DB-wal" "$DB-shm"
    echo "previous database moved aside: $ASIDE"
fi

cp "$BACKUP_DB" "$DB"

echo "restored from $SRC"
echo "  events:   $(sqlite3 "$DB" 'SELECT count(*) FROM events;')"
echo "  revision: $(sqlite3 "$DB" 'SELECT version_num FROM alembic_version;')"
echo
echo "The config and .env in the backup were NOT restored automatically."
echo "Copy them by hand if this is a rebuild rather than a rollback:"
echo "    cp $SRC/home.yaml $SRC/cameras.yaml $ROOT/config/"
echo "    cp $SRC/env.backup $ROOT/.env"
echo
echo "Then: make start"
