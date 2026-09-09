#!/usr/bin/env bash
# A consistent, closed copy of the live database that the host may open freely.
#
# The snapshot is taken INSIDE the container using SQLite's own backup API, so
# there is exactly one process, one kernel and one view of the WAL. Only once
# that copy is complete and closed is it moved to the host.
#
# The rule this exists to enforce: the host must never open the LIVE database
# while hermes-home is running. Measured on this deployment, a host reader saw a
# frozen row count across five consecutive reads while the container committed
# once a second, and 16 committed rows were permanently lost -- with no error
# raised anywhere. A finished snapshot has none of that hazard: nothing else
# holds it, and it has no live WAL.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${CONTAINER:-hermes-home}"
DEST="${1:-$ROOT/data/snapshots}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$DEST/hermes-home-$STAMP.db"

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    echo "hermes-home is not running; nothing holds the database." >&2
    echo "You can inspect ./data/hermes-home.db directly, or start the service." >&2
    exit 1
fi

mkdir -p "$DEST"

# sqlite3's .backup, run in-container, against a path on the same bind mount so
# the finished file lands where the host can reach it without a docker cp.
docker exec "$CONTAINER" sqlite3 "/data/hermes-home.db" ".backup '/data/.snapshot-tmp.db'"
mv "$ROOT/data/.snapshot-tmp.db" "$OUT"

# Verify the copy now that it is closed and exclusively ours.
INTEGRITY="$(sqlite3 "$OUT" 'PRAGMA integrity_check;')"
[ "$INTEGRITY" = "ok" ] || { echo "snapshot failed integrity check: $INTEGRITY" >&2; exit 1; }

echo "snapshot: $OUT"
echo "  integrity:  $INTEGRITY"
echo "  events:     $(sqlite3 "$OUT" 'SELECT count(*) FROM events;')"
echo "  deliveries: $(sqlite3 "$OUT" 'SELECT count(*) FROM event_deliveries;')"
echo "  revision:   $(sqlite3 "$OUT" 'SELECT version_num FROM alembic_version;')"
echo
echo "Safe to open from macOS: it is a closed copy with no live WAL."
echo "    sqlite3 $OUT"

KEEP="${KEEP:-10}"
ls -1t "$DEST"/hermes-home-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f "$old"
done
