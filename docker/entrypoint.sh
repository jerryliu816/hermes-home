#!/bin/sh
# Container startup sequence.
#
# The ordering is the point: back up, migrate, verify, then serve. If any step
# fails we exit non-zero without starting the service, so a bad upgrade leaves
# a recoverable database rather than a half-migrated one being served against.
set -eu

DB_PATH="${DB_PATH:-/data/hermes-home.db}"
BACKUP_DIR="${BACKUP_DIR:-/data/backups}"
KEEP_BACKUPS="${KEEP_BACKUPS:-10}"

log() { printf '%s entrypoint: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# --- 0. Is a migration actually pending? ----------------------------------
# Only back up and migrate when the schema differs from what this image
# expects. Without this check a crash-restart loop would snapshot the database
# every couple of seconds for no reason.
PENDING=1
if [ -f "$DB_PATH" ]; then
    CURRENT=$(alembic current 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1 || true)
    HEAD=$(alembic heads 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1 || true)
    if [ -n "$CURRENT" ] && [ "$CURRENT" = "$HEAD" ]; then
        PENDING=0
        log "database already at $CURRENT; no migration needed"
    else
        log "migration pending: ${CURRENT:-none} -> ${HEAD:-unknown}"
    fi
fi

# --- 1. Back up before touching the schema --------------------------------
# sqlite3 .backup, not cp: with WAL enabled a plain copy can miss committed
# data still sitting in the -wal file.
if [ "$PENDING" = "1" ] && [ -f "$DB_PATH" ]; then
    mkdir -p "$BACKUP_DIR"
    SNAPSHOT="$BACKUP_DIR/pre-migration-$(date -u +%Y%m%d-%H%M%S).db"
    if sqlite3 "$DB_PATH" ".backup '$SNAPSHOT'"; then
        log "pre-migration backup: $SNAPSHOT"
    else
        log "FATAL: could not back up $DB_PATH; refusing to migrate"
        exit 1
    fi
    # Keep the most recent N so a crash-restart loop cannot fill the disk.
    ls -1t "$BACKUP_DIR"/pre-migration-*.db 2>/dev/null \
        | tail -n +"$((KEEP_BACKUPS + 1))" \
        | while read -r old; do rm -f "$old"; log "pruned $(basename "$old")"; done
elif [ ! -f "$DB_PATH" ]; then
    log "no database at $DB_PATH; a new one will be created"
fi

# --- 2. Migrate -----------------------------------------------------------
if [ "$PENDING" = "1" ]; then
    log "applying migrations"
    if ! alembic upgrade head; then
        log "FATAL: migration failed; service will not start."
        log "       The pre-migration snapshot in $BACKUP_DIR is intact."
        exit 1
    fi
fi
log "database at revision $(alembic current 2>/dev/null | grep -oE '^[0-9a-f]+' | head -1 || echo unknown)"

# --- 3. Serve -------------------------------------------------------------
# exec so uvicorn becomes PID 1 and receives SIGTERM directly. Without it the
# shell swallows the signal, shutdown becomes a SIGKILL, and in-flight work is
# abandoned rather than finished.
log "starting hermes-home"
exec "$@"
