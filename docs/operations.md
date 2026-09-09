# Operations

Everything needed to run hermes-home day to day. `make help` lists the commands.

## Quick reference

| Task | Command |
|---|---|
| Start | `make start` |
| Stop | `make stop` |
| Restart | `make restart` |
| Status + readiness | `make status` |
| Liveness | `make health` |
| Recent logs | `make logs` |
| Follow logs | `make follow` |
| Upgrade to new code | `make upgrade` |
| Roll back | `make rollback` (prints the exact steps) |
| Back up | `make backup` |
| Restore | `make stop && make restore FROM=~/hermes-home-backups/<stamp> && make start` |
| Prune old webhook bodies | `make prune` |
| SQL shell | `make db-shell` |
| Shell in container | `make shell` |

## What starts automatically

```
macOS boot
   └─ user logs in
        └─ Docker Desktop starts          (AutoStart is enabled)
             └─ container `hermes-home`   (restart: unless-stopped)
                  └─ entrypoint: backup if needed → migrate → serve
```

`restart: unless-stopped` restarts the container when the process exits
non-zero, and starts it again whenever the Docker engine starts. A deliberate
`make stop` is remembered: the container stays down until `make start`.

**The one manual dependency is the login.** Docker Desktop on macOS runs inside a
user session, so an unattended reboot leaves hermes-home down until someone logs
in. If the machine must recover fully headless, enable automatic login
(System Settings → Users & Groups → Automatic login). This is a real security
tradeoff — an auto-logging-in Mac is an unlocked Mac — so it is left to you
rather than assumed.

Verified: killing the Docker engine and restarting it brought the service back
unaided in about five seconds, healthy, with data intact.

## Health and readiness

They answer different questions and are used differently.

`GET /health` — **liveness.** Is the process alive? Cheap, and deliberately does
not test Home Assistant or the vision provider: reporting unhealthy because a
dependency is down would make Docker restart a service that is working
correctly, and the durable inbox exists precisely so we keep accepting events
while dependencies are unavailable. This is what the container healthcheck uses.

`GET /ready` — **readiness.** Can it actually do its job? Checks the database,
that the schema matches the code, that the worker is running, and that MCP is
mounted. Returns 503 if any fails. Use it after a deploy; do not wire it to a
restart.

```console
$ make ready
{
  "ready": true,
  "checks": {
    "database":   {"ok": true},
    "migrations": {"ok": true, "current": "421395fd4005", "expected": "421395fd4005"},
    "worker":     {"ok": true, "concurrency": 1},
    "mcp":        {"ok": true, "path": "/mcp"},
    "config":     {"ok": true, "cameras": 1}
  }
}
```

## Upgrading

```console
$ make upgrade
```

which backs up, rebuilds, and restarts. On start the entrypoint:

1. Compares the database revision to the one the image expects.
2. If they differ, snapshots the database to `data/backups/pre-migration-*.db`.
3. Applies migrations. **If migration fails it exits non-zero and the service
   does not start**, leaving the snapshot intact.
4. The application then independently refuses to serve if the revision still
   does not match, so a partial upgrade cannot quietly serve against the wrong
   schema.

No migration pending means no snapshot is taken, so a crash-restart loop does
not churn the disk. Snapshots are capped at the most recent 10.

## Rolling back

Code only (schema unchanged):

```console
$ docker images hermes-home            # find the previous tag
$ docker compose up -d                 # after pointing image: at that tag
```

Code and schema (the new version migrated the database):

```console
$ make stop
$ ./scripts/restore.sh ~/hermes-home-backups/<stamp>
$ make start
```

Restoring a pre-migration snapshot from `data/backups/` works the same way.
Note that a rollback across a migration discards events recorded since the
snapshot — check what you would lose first with `make db-shell`.

## Never read the live database from the host

**Do not run `sqlite3 data/hermes-home.db` on the Mac while the container is
running.** Not as a convenience, not for a quick count.

This is measured, not theoretical. With a container committing one row per
second to a WAL database on the bind mount, a host reader saw a **frozen count
across five consecutive backups**, and by the end **16 committed rows had been
permanently lost** — gone from the writer's own connection, gone from fresh
connections, with **no error raised anywhere**. Sixty commits reported success;
forty-four survived.

The WAL index lives in shared memory that virtiofs does not carry across the
host/VM boundary, so the two sides disagree about what is committed, and the
loser is whoever wrote last. The bind mount is not unreliable on its own — the
container wrote to it happily for hours. The hazard is *concurrent host access*.

To inspect the live database, go through the container, where there is one
kernel and one view:

```bash
docker exec hermes-home python -c "import sqlite3; ..."
docker exec hermes-home sqlite3 /data/hermes-home.db "select count(*) from events;"
```

`make backup` now takes its snapshot inside the container automatically, and
falls back to the host only when the container is stopped.

## Backup and restore

`make backup` writes to `~/hermes-home-backups/<UTC timestamp>/`:

| File | Contents |
|---|---|
| `hermes-home.db` | the database, via `sqlite3 .backup` |
| `home.yaml`, `cameras.yaml` | the house description |
| `env.backup` | `.env`, including secrets — mode `600` |

The backup directory is created mode `700` because it contains secrets. Keep it
off shared storage.

**Why `sqlite3 .backup` and not `cp`.** The database runs in WAL mode, so
recently committed transactions can still live in the `-wal` file. Copying only
`hermes-home.db` yields a backup that is silently missing data. `.backup` takes a
consistent snapshot and is safe while the service is running.

Every backup is verified with `PRAGMA integrity_check` before being reported as
successful, and the 30 most recent are kept.

Restoring:

```console
$ make stop
$ ./scripts/restore.sh ~/hermes-home-backups/20260908-175154
$ make start
```

`restore.sh` refuses to run while the service is up — restoring underneath a live
process leaves it holding a stale WAL and can corrupt both copies. It also moves
the current database aside as `hermes-home.db.replaced-<stamp>` rather than
deleting it, so a restore from the wrong snapshot is itself reversible.

Config and `.env` are **not** restored automatically. That is deliberate: the
common case is rolling back data on a working machine, where overwriting live
credentials with older ones would be a surprise. Copy them by hand when
rebuilding from scratch (the script prints the commands).

### Suggested schedule

There is no built-in scheduler. For a nightly snapshot:

```console
$ crontab -e
0 3 * * *  ./scripts/backup.sh >> /tmp/hh-backup.log 2>&1
```

The database is small — a year of events is a few megabytes — so retention is
about having enough history to roll back to, not about space.

## Background maintenance

One loop in the ingest worker handles both periodic chores, ticking at the
shorter cadence rather than running a scheduler per job:

| Job | Default cadence | Setting |
|---|---|---|
| Close settled incidents and summarize them | 60s | `INCIDENT_SWEEP_INTERVAL_SECONDS` |
| Prune raw webhook bodies past retention | hourly | `RETENTION_SWEEP_INTERVAL_SECONDS` |

The camera health monitor runs as a **separate** task, not part of that loop:
health must keep being observed while ingestion is idle, and a failure in one
must not affect the other.

| Job | Default cadence | Setting |
|---|---|---|
| Poll camera availability | 60s | `CAMERA_HEALTH_INTERVAL_SECONDS` |

It logs only transitions, never successful polls:

```console
$ make logs | grep health_changed
  camera.health_changed  camera=backyard old_status=healthy new_status=offline reason=camera_entity_unavailable
  home_assistant.health_changed  old_status=reachable new_status=unreachable
```

Restarting hermes-home deliberately leaves a gap in the coverage record for the
time it was down, reported as `unknown` rather than being papered over. See
[camera-health.md](camera-health.md).


An incident is closed once it has been idle for `INCIDENT_IDLE_SECONDS`
(default 120, matching the correlation window). Closing only sets `status`,
`summary` and `updated_at` — event membership is never touched — and the sweep
is idempotent, so an already-closed incident is skipped rather than rewritten.

Look for `incidents.closed` in the logs:

```console
$ make logs | grep incidents.closed
  incidents.closed  count=9 idle_seconds=120
```

## Durability of a 202

A `202` from the webhook is a promise that the delivery exists. Before making
it, the handler re-reads the row through a **fresh connection that did not
perform the write** — a row is always visible to its own writer, so only an
uninvolved reader can attest that it is really there. If that read fails or
comes back empty, the webhook returns **503** and logs
`webhook.durability_unconfirmed`; `webhook.accepted` is never written.

That matters because Home Assistant's `rest_command` logs a warning on any
non-2xx but nothing at all on success. On 2026-09-09 four deliveries were
committed without error, acknowledged with 202, and then were simply not there
— and because we had already promised success, Home Assistant had no reason to
complain.

There is no retry. If the database cannot confirm a write it just accepted, the
honest move is to fail loudly rather than guess.

```console
$ make logs | grep durability_unconfirmed
```

### What fsync actually guarantees here

`synchronous=FULL` is set, so SQLite fsyncs the WAL on every commit rather than
only at checkpoints. Measured cost on this deployment: **~0.06ms per commit**.

But be clear about the limit. Measured on this machine, `FULL` costs **1.3x**
`OFF` on the `./data` bind mount and **92.7x** `OFF` on the container's own
filesystem. The macOS virtiofs layer acknowledges syncs without durably
flushing to the host disk, so `FULL` does not make power-loss durability real
here — it is set because it is correct, free, and right if the storage ever
changes.

The guarantee this system actually enforces is the one it can verify:
cross-connection visibility, checked per delivery.

## Integrity

`PRAGMA quick_check` runs at startup. The result appears in `/health` as
`integrity` and in `/ready` as a check; a failure makes the service **unready**
but never fails liveness, because restarting a process over a corrupt database
just corrupts it in a loop.

Nothing is repaired automatically. A corrupt database is a situation for a human
and a backup — an automatic rebuild would destroy the evidence of what went
wrong, which is the only thing that makes a recurrence diagnosable. Restore from
`make backup` output; see below.

## Where data lives

| Path | Contents | Survives container removal |
|---|---|---|
| `./data/hermes-home.db` | events, analyses, zones, incidents, the queue | yes (bind mount) |
| `./data/backups/` | automatic pre-migration snapshots | yes |
| `~/hermes-home-backups/` | manual `make backup` snapshots | yes |
| `./config/*.yaml` | house description, mounted read-only | yes |
| `./.env` | secrets, never in the image | yes |
| container filesystem | nothing of value | no |

## Logs

`make logs` / `make follow`. JSON-structured, capped at 5 files of 10 MB.

Every delivery carries a `correlation_id` from acceptance through to
persistence, so one grep reconstructs a single event:

```console
$ docker compose logs | grep <correlation-id>
```

Uvicorn's access log is off — the app already logs each webhook with its client
address, and the 30-second healthcheck would otherwise dominate.

## Troubleshooting

**Container restart-looping.** `make logs` and read the first traceback. The
entrypoint prints its progress, so you can see whether it failed at backup,
migration, or startup. A migration failure exits before serving and leaves the
snapshot in `data/backups/`.

**`ready` is 503 with `migrations.ok: false`.** The database and code disagree.
`make upgrade` applies pending migrations. If the database is *ahead* of the
code, you rolled back the image without rolling back the data — restore a
snapshot from before the upgrade.

**Home Assistant reports connection errors.** Check `LAN_BIND_IP` in `.env`
still matches this machine's address (`ipconfig getifaddr en0`). A DHCP change
moves the service; consider a DHCP reservation.

**Hermes cannot see the tools.** `hermes mcp test hermes-home`. The MCP endpoint
enforces DNS-rebinding protection, so a client connecting by any hostname other
than `localhost`/`127.0.0.1` needs that name in `MCP_ALLOWED_HOSTS`.

**Events accepted but never processed.** `make ready` — if `worker.ok` is false
the queue is not draining. Deliveries are durable, so nothing is lost; they
process once the worker is healthy again.
