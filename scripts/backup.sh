#!/usr/bin/env bash
# Back up the hermes-home database and configuration.
#
# Uses sqlite3 .backup rather than cp. With WAL enabled a plain file copy can
# capture the main database without the -wal file that holds recently committed
# transactions, producing a backup that is silently missing data. .backup takes
# a consistent snapshot and is safe while the service is running.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${DB:-$ROOT/data/hermes-home.db}"
DEST="${1:-$HOME/hermes-home-backups}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$DEST/$STAMP"

[ -f "$DB" ] || { echo "no database at $DB" >&2; exit 1; }
mkdir -p "$OUT"

# Run the snapshot INSIDE the container when one is running.
#
# Reading this database from the host while the container holds it open is not
# safe on a macOS bind mount, and this is measured rather than theoretical: with
# a container committing one row per second, a host reader saw a frozen count
# across five consecutive backups, and 16 committed rows were permanently lost
# -- invisible afterwards even to the connection that wrote them, with no error
# raised anywhere. The WAL index lives in shared memory that virtiofs does not
# carry across the boundary, so the two sides disagree about what is committed
# and the loser is whoever wrote last.
#
# A backup taken from the host is therefore both unreliable (a stale snapshot)
# and actively destructive (it can discard recent transactions). Inside the
# container there is exactly one kernel and one view, and both problems vanish.
CONTAINER="${CONTAINER:-hermes-home}"
if docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    docker exec "$CONTAINER" python -c "
import sqlite3, sys
src = sqlite3.connect('/data/hermes-home.db')
dst = sqlite3.connect('/data/.backup-tmp.db')
with dst:
    src.backup(dst)
dst.close(); src.close()
"
    mv "$ROOT/data/.backup-tmp.db" "$OUT/hermes-home.db"
    echo "  source:    container ($CONTAINER)"
else
    # Nothing else has the database open, so the host is the only reader.
    sqlite3 "$DB" ".backup '$OUT/hermes-home.db'"
    echo "  source:    host (container not running)"
fi

# Configuration: the house description, plus the secrets file. .env is included
# because a restore without it cannot talk to Home Assistant -- so the backup
# directory is itself sensitive and is created private.
cp "$ROOT/config/home.yaml" "$ROOT/config/cameras.yaml" "$OUT/" 2>/dev/null || true
if [ -f "$ROOT/.env" ]; then
    cp "$ROOT/.env" "$OUT/env.backup"
    chmod 600 "$OUT/env.backup"
fi
chmod 700 "$OUT"

# Verify what we just wrote, rather than assume it.
INTEGRITY="$(sqlite3 "$OUT/hermes-home.db" 'PRAGMA integrity_check;')"
[ "$INTEGRITY" = "ok" ] || { echo "BACKUP FAILED integrity check: $INTEGRITY" >&2; exit 1; }

EVENTS="$(sqlite3 "$OUT/hermes-home.db" 'SELECT count(*) FROM events;')"
REV="$(sqlite3 "$OUT/hermes-home.db" 'SELECT version_num FROM alembic_version;')"

echo "backup: $OUT"
echo "  integrity: $INTEGRITY"
echo "  events:    $EVENTS"
echo "  revision:  $REV"

# Retention: keep the most recent N snapshots.
KEEP="${KEEP:-30}"
ls -1dt "$DEST"/*/ 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -rf "$old"; echo "  pruned $(basename "$old")"
done
