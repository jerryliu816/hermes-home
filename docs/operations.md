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
| Roll back | `make rollback` (prints the exact steps; see [Rolling back](#rolling-back)) |
| Back up | `make backup` |
| Restore | `make stop && make restore FROM=~/hermes-home-backups/<stamp> && make start` |
| Prune old webhook bodies | `make prune` |
| SQL shell | `make db-shell` |
| Shell in container | `make shell` |
| Vision status | `make vision-status` |
| Switch to local model | `make vision-local` (`CHECK=1` for a dry run) |
| Switch to Anthropic | `make vision-anthropic` (`CHECK=1` for a dry run) |

## What starts automatically

```
macOS boot
   └─ user logs in
        ├─ Docker Desktop starts          (Settings → General → "Start Docker
        │    │                              Desktop when you sign in" must be on)
        │    └─ container `hermes-home`   (restart: unless-stopped)
        │         └─ entrypoint: backup if needed → migrate → serve
        ├─ Ollama.app starts              (its own login item)
        └─ LaunchAgent local.ollama.limits
             └─ re-applies OLLAMA_MAX_LOADED_MODELS / OLLAMA_NUM_PARALLEL,
                restarting Ollama once if it started without them
```

Nothing has to be run after a reboot. To confirm everything came back —
including which vision provider is live and whether Ollama kept its limits —
run `make vision-status` once you are logged in. It also warns if Docker
Desktop's start-at-sign-in setting has been turned off.

The Ollama pieces only matter while `VISION_PROVIDER=ollama`. If Ollama is not
running, hermes-home still starts and accepts every event; analyses are stored
as failed until Ollama is back, never sent to Anthropic.

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

Compose always runs the image named `hermes-home:0.1.0`. There is no
`IMAGE_TAG` variable, so "running a previous version" means pointing that name
back at a preserved image, then recreating the container. `docker compose
restart` is not enough for anything that changes `.env` or the image: it
reuses the existing container, environment included.

Before any upgrade, preserve the image the container is *actually running* (by
ID, which is right even if `hermes-home:0.1.0` was rebuilt since):

```bash
docker tag "$(docker inspect --format '{{.Image}}' hermes-home)" hermes-home:previous
```

Code only (schema unchanged):

```bash
docker tag hermes-home:previous hermes-home:0.1.0
docker compose up -d --no-build --force-recreate hermes-home
make ready
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

## Local vision (Ollama)

Camera analysis can run on a vision model served by Ollama on this machine
instead of Anthropic. The switch is `VISION_PROVIDER`, and it is global: every
camera uses the one selected provider. There is **no automatic fallback** in
either direction — if Ollama is stopped, the model is missing, or a reply fails
validation, the analysis is stored as `failed` (the event itself is always
kept), and nothing is sent to Anthropic or answered by the mock.

What changes and what does not:

- New analyses record `provider=ollama`, the configured `VISION_MODEL`, and
  `prompt_version=scene-v1+ollama-json-4`. Existing rows are untouched;
  history is never reanalyzed (the images were never stored).
- Freshness, dedupe, the durable inbox, incidents, MCP responses and
  unknown/null semantics are the same code path for every provider.
- No database migration is involved in switching or rolling back.
- `/health` and `/ready` do not check Ollama. A stopped model server must make
  analyses fail visibly, not restart a service that is still accepting events.
  Readiness proves the service is wired, never that inference works.
- `cost_usd` is `0.0`: no per-call bill. Hardware, electricity and the memory
  the model holds are real costs that this column does not measure.

### How a request is bounded

One analysis attempt is one native `POST /api/chat` (`stream: false`) with the
frame as bare base64, the `SceneObservation` JSON schema as `format` (every
key required, nullable fields still nullable), and
`options: {num_ctx, num_predict, temperature: 0}`. The whole attempt — waiting
behind other Ollama clients, a cold model load, generation — runs under
`VISION_TIMEOUT_SECONDS`. The pipeline makes at most two attempts, two seconds
apart, and only retries timeouts, 429, 5xx and connection failures. A missing
model (404), an unsupported option/image/schema (400), or a reply that is not a
complete, valid observation is terminal.

A reply is accepted only if `done` is true, `done_reason` (when present) is
`stop`, the echoed `model` is exactly the configured one, and `message.content`
validates as a `SceneObservation` with no extra fields. `message.thinking` is
never read. Nothing is repaired or defaulted beyond the schema's own nulls.
Ollama has no refusal signal, so nothing maps to `refused`; a model that
answers in prose is a `bad_response`.

Lease arithmetic, enforced at startup when `VISION_PROVIDER=ollama`:

```
freshness (15 x 1s) + 2 x VISION_TIMEOUT_SECONDS + 2s retry + 15s margin < INGEST_LEASE_SECONDS
15 + 2 x 90 + 2 + 15 = 212s  <  300s
```

`DELIVERY_SETTLE_SECONDS` does not need to change: reconciliation matches
triggers to the time each webhook was *received*, which happens before any
vision work. `SHUTDOWN_GRACE_SECONDS` (30) stays under the Compose stop grace
(60). A job still analyzing at shutdown is cancelled; its delivery keeps its
lease and is reclaimed when the lease expires (up to `INGEST_LEASE_SECONDS`
later). The reclaimed job fetches the camera's *current* event image, which may
be a later event's or be rejected as stale — the original frame was never kept,
so time spent down cannot be recovered by any timeout setting.

### 1. Probe Ollama from inside the running container (metadata only)

Proves the container can reach native Ollama, and what Ollama reports about the
model. No inference, no image, no environment printed:

```bash
docker compose exec -T hermes-home python -c "
import httpx
base = 'http://host.docker.internal:11434'
print('version', httpx.get(base + '/api/version', timeout=5).json())
show = httpx.post(base + '/api/show', json={'model': 'gemma4:31b'}, timeout=5).json()
print('capabilities', show.get('capabilities'))
print('details', {k: show.get('details', {}).get(k) for k in ('family', 'format', 'quantization_level', 'runner')})
"
```

`capabilities` must include `vision`. If it includes `thinking`, use
`OLLAMA_THINK=false`; if not, `OLLAMA_THINK=omit` (Ollama rejects the flag for
models without the capability). Also from the host: `ollama list` and
`ollama show gemma4:31b`. Do not change Ollama's bind address — the container
reaches it through Docker Desktop's `host.docker.internal`.

### 2. Back up and preserve the current image and settings

```bash
make backup
docker tag "$(docker inspect --format '{{.Image}}' hermes-home)" hermes-home:pre-ollama
docker image inspect --format '{{.Id}}' hermes-home:pre-ollama   # record this ID
install -m 600 .env ~/hermes-home-backups/env.pre-ollama        # private; never cat it
```

The Anthropic key stays in that private copy (and in `.env`, unused while
Ollama is selected); there is no reason to delete or rotate it for this.

### 3. Build and check a candidate image without touching production

```bash
docker build -t hermes-home:ollama-candidate .
```

Config check in a throwaway container: no `.env`, no `data/` mount, entrypoint
overridden so no migration or backup runs against anything:

```bash
docker run --rm --entrypoint hermes-home \
  -e VISION_PROVIDER=ollama -e VISION_MODEL=gemma4:31b \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 -e OLLAMA_THINK=false \
  -e VISION_TIMEOUT_SECONDS=90 -e INGEST_LEASE_SECONDS=300 \
  hermes-home:ollama-candidate vision check
```

### 4. Opt-in live probe (real network, synthetic frame)

**This is a real inference call**, unlike the test suite, whose Ollama tests
all run against a mocked transport. It loads `gemma4:31b` (about 19 GB, as a
separate runner from Hermes's `gemma4-64k`) and sends one *generated* 320x240
frame — sky over ground, no household content. It writes nothing and never
opens the database:

```bash
docker run --rm --entrypoint hermes-home \
  -e VISION_PROVIDER=ollama -e VISION_MODEL=gemma4:31b \
  -e OLLAMA_BASE_URL=http://host.docker.internal:11434 -e OLLAMA_THINK=false \
  -e OLLAMA_KEEP_ALIVE=30m -e VISION_TIMEOUT_SECONDS=90 -e INGEST_LEASE_SECONDS=300 \
  hermes-home:ollama-candidate vision probe
```

Run it twice: the first includes a cold load, the second is warm. Success
means `status: ok` with a full observation — proof that this runner accepts an
image together with a JSON schema and that the reply validates. It is not
evidence of accuracy. To check a camera's prompt (e.g. Gecko's
`vision_context`) with the same synthetic frame, add
`-v "$PWD/config:/app/config:ro"` and `--camera gecko`. To use a public test image
instead, mount it read-only and pass `--image /probe/frame.jpg`. Do not pass a
household image without deciding to send it to the model.

If the probe fails with `HTTP 400`, the runner rejected an option, the image or
the schema; try `OLLAMA_THINK=omit` before anything else.

### Switching with one command

`scripts/vision.sh` (or the `make vision-*` targets) does steps 2–7 below as
one command each:

```bash
make vision-local CHECK=1     # every check, nothing changed
make vision-local             # switch new analyses to gemma4:31b
make vision-status            # what is live, last 5 analyses, Ollama, memory
make vision-anthropic         # switch back
```

A switch:

1. Checks the image. On first activation, when `hermes-home:0.1.0` predates
   local vision, it uses `hermes-home:ollama-candidate` and keeps the old image
   tagged `hermes-home:pre-ollama`.
2. Checks the provider: Ollama has the model with vision (and warns if Ollama
   keeps fewer than 2 models loaded), or the Anthropic key is present — never
   displayed.
3. Writes the new `.env` to a private file, changing only the vision and
   timing keys, and validates it in a throwaway container with no database.
   For Ollama it also checks the container can reach the model.
4. Backs up the database (`scripts/backup.sh`).
5. Saves the current `.env` as `~/hermes-home-backups/env.before-<provider>-<stamp>`,
   installs the new one, recreates the container, and waits up to 90 s for
   readiness.
6. If the service does not come back ready, restores the saved `.env` and
   recreates again, then exits with an error.

The Anthropic switch restores the timing Anthropic always ran with
(`VISION_TIMEOUT_SECONDS=30`, `INGEST_LEASE_SECONDS=120`) and leaves the
`OLLAMA_*` lines in place, ignored. Both providers run on the same image, so
switching back does not need the old image. `LOCAL_MODEL=... make vision-local`
selects a different installed vision model.

A switch is not proof. Only a newly stored analysis with the new provider is
(step 6 below); `make vision-status` shows the last five.

The manual steps, kept for reference and for recovery when the script cannot
run:

### 5. Activate (human-run, after review)

Edit `.env` (do not print it) so it contains:

```dotenv
VISION_PROVIDER=ollama
VISION_MODEL=gemma4:31b
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_NUM_CTX=8192
OLLAMA_NUM_PREDICT=2048
OLLAMA_THINK=false
OLLAMA_KEEP_ALIVE=30m
VISION_TIMEOUT_SECONDS=90
INGEST_WORKER_CONCURRENCY=1
INGEST_LEASE_SECONDS=300
```

Then point the Compose image name at the reviewed candidate and recreate:

```bash
docker tag hermes-home:ollama-candidate hermes-home:0.1.0
docker compose up -d --no-build --force-recreate hermes-home
make ready
docker compose logs --since 5m hermes-home | grep -E 'service.started|vision|ERROR'
```

`service.started` should show `vision_provider: ollama` and the model. A
`ConfigError` at startup means the model name or lease arithmetic was refused;
fix `.env` and recreate again, or roll back.

### 6. Verify with a real, freshly stored analysis

`/ready` and a webhook `202` prove nothing about inference. Trigger a camera
(walk past the front door), wait a minute, then:

```bash
make db-query SQL="select e.uid, e.occurred_at, a.attempt, a.status, a.provider, a.model, a.prompt_version, a.latency_ms, a.error_code from event_analyses a join events e on e.id = a.event_id order by a.id desc limit 5"
```

and confirm through Hermes with the MCP tool `home_get_event` on that event's
uid: the analysis must show `provider: ollama`, `model: gemma4:31b`,
`prompt_version: scene-v1+ollama-json-4` and status `ok`. Until such a row
exists, local vision is not active, whatever the configuration says.

### 7. Roll back

```bash
install -m 600 ~/hermes-home-backups/env.pre-ollama .env
docker tag hermes-home:pre-ollama hermes-home:0.1.0
docker compose up -d --no-build --force-recreate hermes-home
make ready
```

Then confirm the next stored analysis shows `provider: anthropic`. No database
restore is needed: the schema did not change. Analyses recorded while Ollama
was active stay in history with their own provenance.

### Evaluating real-image quality

One good probe is not evidence of unattended security quality. Before relying
on it, collect real events under each condition, with your explicit decision
to send those frames to the local model (they are still analyzed and
discarded; nothing extra is retained):

- daylight, dusk and full darkness (IR) at the front door;
- partial occlusion (porch column, someone at the frame edge);
- packages left and packages collected;
- vehicles in view, animals in view;
- the Gecko enclosure: gecko visible, hidden, and an empty-looking enclosure.

For each, compare the stored observation with what a person sees in the live
camera at that moment, and record a human rating per field (counts, package,
lighting, nulls used correctly versus guessed zeros). That rating is the
accuracy measure; the model's own `overall_confidence` is a different thing and
is not evidence of correctness. Past events cannot be reanalyzed, because
their images were discarded; evaluation needs new events.

### Running alongside Hermes's model

Hermes uses `gemma4-64k` (64K context); the camera uses `gemma4:31b` at 8K.
They are different models to Ollama, so with Ollama's default of **one loaded
model** each request evicts the other. Measured that way, single camera
analyses took 25–80 s (the 90 s deadline nearly reached) and Hermes reloaded
after every camera event.

Ollama's server must allow both to stay loaded. These are Ollama-wide settings,
outside this repository, and a deliberate operator choice:

```bash
launchctl setenv OLLAMA_MAX_LOADED_MODELS 3
launchctl setenv OLLAMA_NUM_PARALLEL 1
# then quit Ollama from the menu bar and reopen it
grep "server config" ~/.ollama/logs/server.log | tail -1 | tr ' ' '\n' | grep -E 'MAX_LOADED|NUM_PARALLEL'
```

`launchctl setenv` does **not** survive a logout or reboot; Ollama would then
start with one loaded model again and the eviction behaviour return. On the
Studio this is handled by a LaunchAgent outside the repository,
`~/Library/LaunchAgents/local.ollama.limits.plist`, which re-applies both values
at login, then checks Ollama's own `server config` log line and restarts
Ollama once if it had already started with the defaults. Its log is
`~/Library/Logs/ollama-limits.log`. After a reboot, confirm with the `grep`
above.

To undo:

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.ollama.limits.plist
rm ~/Library/LaunchAgents/local.ollama.limits.plist
launchctl unsetenv OLLAMA_MAX_LOADED_MODELS
launchctl unsetenv OLLAMA_NUM_PARALLEL
# then quit Ollama from the menu bar and reopen it
```

Measured on the Mac Studio (M5 Max, 128 GB, Ollama 0.40.13, MLX runner) with
`OLLAMA_MAX_LOADED_MODELS=3`, `OLLAMA_NUM_PARALLEL=1`, two runs of six real
frames overlapping long Hermes-model generations:

| | Result |
|---|---|
| Both models stayed loaded | yes, every 1 s sample; no reload (`load_duration` ≈ 0) after first load |
| Camera, warm, no overlap | 2.6–4.2 s |
| Camera while the Hermes model generates | 4.9–10.7 s |
| Camera, cold load while Hermes model resident | 7.6 s (1.2 s load) |
| Hermes model while camera requests run | ≈ 26 tokens/s vs ≈ 36 alone (~28 % slower during overlap); never reloaded |
| Valid observations | 14 of 14 |
| Resident size | camera ≈ 25.5 GB, Hermes model ≈ 20–28 GB (both grow with use) |
| Memory pressure | normal while idle or while only one model generates; **brief critical spikes (1–3 s, 18–20 % free) when both generate at once**, each adding ~5 GB of swap |

The critical spikes need simultaneous generation on both models: a long
Hermes-model generation with no camera traffic stayed at normal pressure.
Nothing failed and nothing was evicted, but repeated overlap accumulates swap.
`OLLAMA_MAX_LOADED_MODELS=3` would also let a third model load beside these two;
`qwen3-coder-next` alone is ~51 GB, so loading it while both are resident was
not tested and is likely to push the machine into sustained swapping.

Timing review against these numbers: the worst measured analysis (10.7 s) is
far inside `VISION_TIMEOUT_SECONDS=90`. Keep 90 rather than lowering it: if the
loaded-model limit reverts to 1 (e.g. after a reboot), evict-and-reload
analyses were measured at up to 80 s, and those should still complete rather
than fail. The lease budget is then 15 + 2 × 90 + 2 + 15 = 212 s, under
`INGEST_LEASE_SECONDS=300`. A camera request queues only behind other requests
for `gemma4:31b` itself (one worker, `NUM_PARALLEL=1`), never behind Hermes's
model. A normal job (≈4 s freshness + 3–11 s analysis) finishes well inside
`SHUTDOWN_GRACE_SECONDS=30`. `DELIVERY_SETTLE_SECONDS` stays at 90.

### What to measure

- Cold and warm total latency: `latency_ms` in `event_analyses`, and the probe.
- Timeout and failure rate: `select provider, status, error_code, count(*) from event_analyses group by 1,2,3`.
- Queue behavior during bursts: `make status` (queue counts) and the gap
  between `received_at` and the analysis `completed_at`.
- `ollama ps` during and after events: which runners are loaded, their size,
  and when the camera model unloads.
- Memory pressure (Activity Monitor → Memory, or `memory_pressure`) with the
  camera model, Hermes's model and any coding model loaded at once.
- Latency while Hermes or a coding model is generating: Ollama may queue the
  camera request behind them, and that wait counts against the deadline.

## Never read the live database from the host

> **DO NOT open `data/hermes-home.db`, `-wal` or `-shm` directly from macOS while
> hermes-home is running.**
>
> The database is SQLite in WAL mode inside Docker. Direct host access across
> Docker Desktop's virtiofs boundary has been **reproduced causing committed
> transactions to disappear with no error**.

Not as a convenience, not for a quick count.

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

### Supported commands

Everything below runs the SQLite access inside the container. Use these; there
is no reason to reach for `sqlite3` on the Mac.

| Need | Command |
|---|---|
| One query | `make db-query SQL="select count(*) from events"` |
| Interactive shell (read-only) | `make db-shell` |
| Interactive shell (writable) | `make db-shell-rw` |
| Integrity check | `make db-check` |
| Backup | `make backup` |
| Host-readable copy | `make db-snapshot` |

`make db-snapshot` takes the copy in-container with SQLite's backup API and only
then moves the finished, closed file to `./data/snapshots/`. That copy has no
live WAL and nothing else holds it, so **the host may open it freely** — it is
the supported way to do host-side analysis.

```console
$ make db-snapshot
snapshot: .../data/snapshots/hermes-home-20260909-215152.db
  integrity:  ok
  events:     27

$ sqlite3 data/snapshots/hermes-home-20260909-215152.db   # safe: a closed copy
```

`make backup` also snapshots inside the container, falling back to the host only
when the container is stopped. `make restore` refuses to run at all while the
service is up.

A test enforces this: `tests/test_host_db_access_guard.py` fails if any
committed command opens the live database from the host. The three legitimate
host-side reads (after the service is stopped) carry an explicit `host-db-ok`
marker stating why.

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
