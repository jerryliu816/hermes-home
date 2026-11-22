# hermes-home

Turns the cameras, thermometers and thermostat around a house into something
you can ask questions of — and lets you set the thermostat while you are at it.

```
You:     What happened at the front door?

Hermes:  At about 1:43 PM, a man in a light-blue long-sleeve shirt and khaki
         shorts stood near the front-entry shrubbery looking upward, then walked
         away along the path. No package, vehicle, or animal was detected. The
         camera model rated this medium-to-high confidence.

         Earlier, around 11:52 AM, another person walked by carrying a plastic
         bag containing printed material.
```

A real exchange, from real camera events. Hermes made one tool call —
`home_recent_events(camera="front_door")` — and everything in the answer came
back from it: the times, the descriptions, the absence of a vehicle, and the
confidence.

"No package" there is a real observation, not an assumption: the stored record
says `package_present: false` because the model could see there wasn't one. Had
the frame been too dark to tell, it would have been stored as *unknown* — a
distinct value — and Hermes would have said it didn't know rather than reporting
an all-clear. The confidence remark is volunteered unprompted, because every
observation carries the provider, model and confidence that produced it.

The same works for temperature and for the thermostat (an illustration; the
values are made up):

```
You:     Was the gecko enclosure too cold last night?

Hermes:  No. It stayed inside its night range (62–80 °F) all night: lowest
         68.9 °F at 5:40 AM, averaging 71.2 °F. The thermometer reported
         throughout, so there are no gaps in that.

You:     It's chilly in here. Set the hallway to 68.

Hermes:  Done. The Nest now reports heat mode with a setpoint of 68 °F (it was
         65 °F) and is heating.
```

The first answer comes from `home_sensor_history(sensor="gecko_enclosure")`.
It reports both how low the temperature went and whether the sensor was heard
from the whole time, because a silent thermometer is not a warm one. The second
comes from `home_set_thermostat(temperature=68)`. That tool refuses anything
outside the setpoints you allow, and it reports "done" only after the
thermostat itself shows the change.

## The problem

A camera that spots someone sends a notification and forgets. Home Assistant
knows what every device is doing *right now*, which is a different thing from
knowing what happened at 3pm. Neither keeps a record you can reason over, so the
questions people actually ask have nowhere to go:

- *What happened at the front door?*
- *Did a package arrive while I was out?*
- *Was there anything in the backyard overnight?*
- *How many people came to the house today?*

Answering those needs three things a camera event does not carry: a **record**
that outlives the notification, an **interpretation** of what was in the frame,
and a **place** to attach it to.

You can point a language model at Home Assistant and it will happily read
current entity states — but with no history to read, it either says "I don't
know" or starts guessing. Adding a vision model to each alert produces prose
that is never stored, so nothing accumulates.

## What this does

Sits between Home Assistant and an AI agent, and does the part neither should
have to:

1. **Catches the event** the instant Home Assistant fires it, and commits it to
   durable storage before acknowledging — so an event is never lost to a crash,
   restart, or an offline vision provider.
2. **Fetches the still** the camera captured for that event, waiting for it to
   actually arrive rather than grabbing the previous frame.
3. **Interprets it** with a vision model into structured fields — people,
   vehicles, packages, activity, lighting — not prose. The model is your
   choice: Anthropic in the cloud, or a vision model running locally in
   Ollama, switched with one command.
4. **Throws the image away**, keeping the interpretation.
5. **Files it in place and time**: which zone, which camera, what else was
   happening nearby.
6. **Exposes it to an agent** as twelve deterministic tools, so plain-language
   questions become real queries against real observations — each answer
   carrying whether the cameras behind it were actually working.

### Temperature sensors

Alongside the cameras, it keeps a record of **sensor readings**: thermometers,
hygrometers, batteries, or anything else Home Assistant reports as a number.
This house has two ThermoPro Bluetooth thermometers (the gecko enclosure and
the bedroom), and any make works the same way.

- **Pulled, not pushed.** Every minute it copies new readings from Home
  Assistant's recorder. No HA automation is needed, and downtime loses nothing
  HA still holds.
- **Kept for a year**, where HA forgets after ten days. A newly added sensor
  comes with up to ten days of history copied from HA.
- **Placed and judged.** Each sensor is in a zone and can have a target range,
  plus an optional night range that may cross midnight. History reports every
  span spent outside the range.
- **Honest about silence.** If a sensor dropped out, the span is reported as
  *unknown* with a reason, and it is left out of every minimum, maximum and
  average rather than filled in.

Adding one is a pairing in HA, then `make sensors-discover` to paste a stub
into `config/sensors.yaml`, then `make restart`. `make sensors-status` shows
the latest reading of each. See [docs/sensors.md](docs/sensors.md).

### Nest thermostat

The Hallway Google Nest (`climate.hallway_hallway`, through HA's Nest
integration) is a sensor like the others, with two additions:

- **History.** Four measurements are recorded the same way as the
  thermometers: `temperature`, `humidity`, `setpoint` (what it was set to), and
  `heating`. `heating` is 1 while the heat runs and 0 otherwise, so its average
  over a period is the share of time the heat was on. "How long did the heat
  run last night?" is a `home_sensor_history` call.
- **Control.** `home_thermostat_status` reads it live: mode, whether it is
  heating now, setpoint, preset, and the latest changes.
  `home_set_thermostat` sets a temperature, a low/high range in `heat_cool`,
  the mode, or a preset such as eco.
  - The guardrails are in `config/sensors.yaml`: setpoints only within your
    limits (60–76 °F here) and only the modes you allow.
  - It refuses a change the thermostat would silently ignore, such as a
    setpoint while it is off or in eco, and says why.
  - A change counts as made only when the thermostat reports it back.
  - Every request, refused ones included, is recorded in `thermostat_changes`.

`make thermostat-status` shows the same thing from the command line.

## What that buys you

**Memory instead of alerts.** A month of events stays queryable. "How often does
anyone come to the door on a weekday?" becomes an answerable question rather
than a scroll through notifications.

**Answers that are grounded, and honest when they are not.** Every observation
records which provider, model and confidence produced it, so the agent can
qualify a weak reading instead of stating it flatly — as it does above, without
being asked. Run the mock provider and it will tell you the interpretation is
synthetic.

**Unknown never becomes zero.** If the model could not tell how many people were
in a dark frame, that is stored as *unknown*, not as `0`. Counts and averages
skip it rather than quietly counting it as "nobody was there." Surprisingly few
systems get this right, and it is the difference between a summary you can trust
and one you cannot.

**It knows what it cannot see.** Zones no camera watches are recorded as such,
so "no events in the backyard" is reported as *no coverage there*, never as an
all-clear.

**It knows when events went missing.** Camera health cannot tell you that Home
Assistant fired an event which never arrived — that failure looks identical to
silence. So deliveries are reconciled against Home Assistant's own recorder, and
a query over an affected period reports the events as *missing*, with the exact
timestamps, rather than as *nothing happened*.

**It knows when it was not looking.** Camera health is polled and kept as
history, so a quiet night and a camera that was offline from 3:17 to 5:42 are
different answers. Periods before monitoring began, or while the service was
down, come back as *unknown* — never as "all clear". Absence of an outage record
is never treated as evidence of health.

**Privacy by construction, not by policy.** Images are analyzed and discarded —
what persists is a description. There is no facial recognition, no biometric
matching, no identity tracked across events, and the data model has nowhere to
put one. "A person in a blue jacket" is the intended register; "this is Jerry"
is not expressible.

**Cloud or local, and never silently both.** Camera analysis runs on Anthropic
or on a vision model served by Ollama on the same machine — `make vision-local`
and `make vision-anthropic` switch between them, validating first and undoing
themselves if the service does not come back. With the local model, frames
never leave the house and there is no per-event bill. There is no fallback in
either direction: if the selected model is down, the event is still recorded
and its analysis is marked failed, rather than quietly answered by a different
model. Every analysis says which provider, model and prompt version produced
it, so history stays interpretable across a switch.

**Vendor-neutral.** Home Assistant is the device abstraction, so there is no
Eufy code here. Replacing every camera with another brand changes entity IDs in
a YAML file. Adding a different *kind* of device later — a Powerwall, a door
sensor — is a new event type, not a schema change, because the event table never
knew what a camera was.

**Boring to run.** One container, one SQLite file on the host, no broker, no
external services. It restarts itself, comes back on its own after a reboot,
backs up with `make backup`, and `make vision-status` shows in one screen what
is running, what it last analyzed, and with which model.

> One operational rule, learned the hard way: **never open the database from
> macOS while the container is running.** Use `make db-query`, `make db-shell`,
> `make db-check`, or `make db-snapshot` for a host-readable copy. Direct host
> access across Docker's virtiofs boundary has been measured destroying
> committed transactions silently — see
> [operations.md](docs/operations.md#never-read-the-live-database-from-the-host).

## What it is not

It does not replace Home Assistant or watch live video, and apart from the
thermostat it controls no device. It stores no footage. It answers questions
about the past. Current device state and device control stay with Home
Assistant, where they belong. The one exception is the thermostat, which it
can set through one guarded tool (above).

## How the pieces fit

Home Assistant is the device layer. Hermes is the reasoning layer. This service
is the bit in the middle that neither of them should have to be: it turns camera
events into structured, timestamped, located observations and keeps them.

```
   Physical devices  (Eufy cameras, ThermoPro thermometers, Nest thermostat,
          |           later a Powerwall)
          |
          v
   Home Assistant           device connectivity, entity state, automations
          |
          |  webhook (metadata only, acknowledged in a few ms)
          v
   hermes-home              +---------------------------------------+
          |                 |  durable inbox  ->  background worker |
          |                 |     freshness gate                    |
          |                 |     fetch event still from HA         |
          |                 |     content dedupe                    |
          |                 |     vision analysis (Anthropic or     |
          |                 |       local Ollama, one at a time)    |
          |                 |     discard the image                 |
          |                 |     persist event + analysis          |
          |                 |  sensor recorder (pulls HA history    |
          |                 |     every minute: thermometers,       |
          |                 |     thermostat setpoint + heating)    |
          |                 |  thermostat control (checked against  |
          |                 |     limits, climate.* calls to HA,    |
          |                 |     read back, audited)               |
          |                 +---------------------------------------+
          |                              |
          |                              v
          |                        SQLite  (events, analyses, zones, incidents,
          |                                 sensor history, thermostat changes)
          |                              |
          |  MCP tools (semantic; read-only except home_set_thermostat)
          v
   Hermes Agent             "What just happened at the front door?"
```

## Why it is built this way

**Home Assistant must never wait on us.** The webhook authenticates, validates,
commits the delivery, and returns `202` — everything expensive happens
afterwards on a background worker. If the vision provider is down, or this
service is restarting, or the machine is off, the HA automation still completes
promptly and the house keeps working.

**Durability without infrastructure.** The delivery is committed *before* the
202 is sent, and SQLite in WAL mode is the queue. Claiming a job is one atomic
`UPDATE` with a lease, so a worker killed mid-job leaves work that becomes
claimable again on restart. No Redis, no Celery, no broker. Exactly-once comes
from a `UNIQUE` constraint on the event's delivery key, not from the queue.

**Events are immutable; everything opinionated lives elsewhere.** Vision analysis
is a separate table (one row per attempt, so a retry adds a row instead of
destroying the failure that preceded it). Incidents are derived and disposable.
The `events` table itself knows nothing about cameras — which is what makes
adding Powerwall energy events later a new Pydantic model rather than a
migration.

**Unknown is not zero.** A count the model could not determine is stored as SQL
`NULL`, never `0` and never `-1`. `person_count > 0` and `person_count = 0` then
both correctly exclude it, and aggregates skip it. "Unknown number of people"
can never silently become "nobody was there."

**No identity.** No facial recognition, no biometric identification, no plate
database. The observation schema is `extra="forbid"` and has no name or identity
field, so the model has nowhere to record who someone is even if it tried.
`"a person in a blue jacket carrying a box"` is the intended register.

**Images are not kept.** Retrieve, analyze, discard. What survives is the
content hash, dimensions, provider, model, prompt version, latency, tokens, and
the structured observation.

## Quick start

```bash
git clone <this repo> && cd hermes-home

cp .env.example .env                     # HA URL + token, webhook secret
                                         # and LAN_BIND_IP (this machine's LAN
                                         # address — required, left blank)
cp config/home.example.yaml config/home.yaml
cp config/cameras.example.yaml config/cameras.yaml   # your entity IDs
cp config/sensors.example.yaml config/sensors.yaml   # optional: thermometers, thermostat

make start                               # build, migrate, run
make status                              # container state + readiness
```

Then follow [docs/home-assistant-setup.md](docs/home-assistant-setup.md) to add
the automation, and [docs/operations.md](docs/operations.md) for day-to-day
commands.

For local development without Docker:

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev,anthropic,mcp]"
.venv/bin/hermes-home check-config       # fails loudly if anything is missing
.venv/bin/hermes-home db upgrade
.venv/bin/hermes-home serve
```

It runs with **no API keys at all** — the default vision provider is a
deterministic mock. Point it at a real model only when you are ready.

## Configuration

Secrets and deployment settings live in `.env` (gitignored). The non-secret
description of the house — zones, how they connect, which camera watches what —
lives in `config/home.yaml` and `config/cameras.yaml`. Thermometers and the
thermostat live in `config/sensors.yaml`, which is optional: without it, nothing
is synced and nothing can be set. A sensor's `thermostat:` block names the
`climate.*` entity, the setpoint limits and the allowed modes:

```yaml
  hallway:
    name: Hallway Thermostat
    aliases: [thermostat, nest]
    location: hallway
    thermostat:
      entity: climate.hallway_hallway
      setpoint: {min: 60, max: 76}       # the only setpoints Hermes may choose
      modes: [heat, cool, heat_cool, off]
    measurements:
      temperature: {entity: sensor.hallway_hallway_temperature, unit: "°F"}
      humidity:    {entity: sensor.hallway_hallway_humidity}
      setpoint:    {entity: climate.hallway_hallway, attribute: temperature, unit: "°F"}
      heating:     {entity: climate.hallway_hallway, attribute: hvac_action,
                    states: {heating: 1, idle: 0, off: 0, cooling: 0}}
```

Both are validated at startup. A camera pointing at a zone that does not exist
fails immediately with the offending name, rather than at 3am on a real event.

### Vision providers

| `VISION_PROVIDER` | Needs | Recorded `prompt_version` | Notes |
|---|---|---|---|
| `mock` (default) | nothing | `scene-v1` | Deterministic, seeded from the image hash. Fully functional. |
| `anthropic` | `ANTHROPIC_API_KEY` | `scene-v1` | Structured output via the Messages API, with prompt caching. |
| `ollama` | Ollama running natively, and an installed vision model in `VISION_MODEL` (e.g. `gemma4:31b`) | `scene-v1+ollama-json-4` | Native `/api/chat` with the observation JSON schema as `format`. No key, no per-call cost. |

Exactly one provider handles every camera, and there is no fallback: if the
selected provider is down, the analysis is stored as failed and the event is
kept. Analyses already stored keep the provider, model and prompt version they
were made with; nothing is reanalyzed (the images were never kept).

`VISION_MODEL` defaults to `claude-sonnet-5` ($2/$10 per Mtok). This fires on
every camera event, so the cost knob is deliberate and is one line:
`claude-opus-5` ($5/$25) if accuracy disappoints, `claude-haiku-4-5` ($1/$5)
once volume grows. With `VISION_PROVIDER=ollama`, startup refuses a `claude-*`
model name: choosing a local model has to be deliberate.

#### Local vision with Ollama

The Ollama provider holds a local model to the same contract as Anthropic, and
is stricter about what counts as an answer:

- **Same prompt, same schema.** It uses the shared prompt builder, including
  each camera's `vision_context`, plus a short Ollama-only instruction (what
  each lighting value means, suggested activity labels, and that a frame
  darkness hides keeps its counts *unknown*). That extra text is why its rows
  record a distinct prompt version.
- **Every field answered or explicitly unknown.** The schema sent to Ollama
  marks every key required while keeping nullable fields nullable. Without
  that, a local model simply left fields out and they were recorded as
  unknown when the model had never said so.
- **Only complete, valid replies count.** The reply must be finished
  (`done`, `done_reason: stop`), name the model that was asked for, and
  validate as an observation with no extra fields. Prose, fenced JSON, a
  truncated reply or a reasoning trace is a failed analysis, never repaired.
- **Bounded.** One deadline (`VISION_TIMEOUT_SECONDS`) covers queueing behind
  other Ollama clients, a cold model load and generation. Only timeouts, busy
  and server errors and connection failures are retried, once, by the
  pipeline. A missing model or an unsupported option fails immediately.
- **Nothing private in errors.** Stored error messages are fixed strings: no
  response body, prompt, camera context or image data.

| Setting | Default | For local vision | Purpose |
|---|---|---|---|
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | `http://host.docker.internal:11434` | Native API base (never `/v1`); the container reaches the host's Ollama this way |
| `OLLAMA_NUM_CTX` | `8192` | `8192` | Context for the camera request only, not Ollama-wide |
| `OLLAMA_NUM_PREDICT` | `2048` | `2048` | Output token limit; a reply cut off here is rejected |
| `OLLAMA_THINK` | `omit` | `false` | `omit`, `true` or `false`; `false` for thinking-capable models |
| `OLLAMA_KEEP_ALIVE` | unset | `30m` | Per-request keep-alive; never changes Ollama's own settings |
| `VISION_TIMEOUT_SECONDS` | `30` | `90` | Total deadline per attempt |
| `INGEST_LEASE_SECONDS` | `120` | `300` | Must exceed freshness + 2 × timeout + retry + margin; startup refuses otherwise |

You do not normally set these by hand: `make vision-local` applies the
right-hand column and `make vision-anthropic` restores Anthropic's values (see
[Switching the vision provider](#switching-the-vision-provider)).

**Running beside another local model.** If something else on the machine uses
a different Ollama model — Hermes does — Ollama must be allowed to keep both
loaded (`OLLAMA_MAX_LOADED_MODELS` of at least 2), or each request evicts the
other and analyses slow to 25–80 s. On the reference Mac Studio this is set
at every login by a small LaunchAgent; the setup, measured latency and memory
behaviour are in
[docs/operations.md](docs/operations.md#running-alongside-hermess-model).

**Which model.** On six real frames (one per camera; a small sample, not an
accuracy measurement) `gemma4:31b`'s person, vehicle, animal and package
counts matched a manual reading of every frame and left the dark Gecko frame's counts unknown, as it
should. `qwen3.6:35b-a3b` was faster but left many readable counts unknown and
rated routine scenes high importance. Anthropic's descriptions were the
richest, but it gave different counts for the same frame across runs, and it
costs per event. `gemma4:31b` is the recommended local model; judge it on your
own events before relying on it unattended — the evaluation plan is in
[docs/operations.md](docs/operations.md#evaluating-real-image-quality).

### Retention

| Data | Kept | Config |
|---|---|---|
| Event images | **never stored** | — |
| Raw webhook bodies | 14 days, then nulled | `DELIVERY_RAW_RETENTION_DAYS` |
| Delivery metadata (time, status, disposition, errors) | permanently | — |
| Events, analyses, tags, incidents | permanently | — |

`hermes-home db prune` runs the sweep manually; the worker also runs it hourly.

## Switching the vision provider

One command each way, run from the repository on the host. Each one checks
everything before it changes anything, and puts things back if the service
does not come back ready.

| Command | Does |
|---|---|
| `make vision-status` | What is running and on which image/commit, the provider in use vs. `.env`, readiness, the last five analyses, Ollama's version, limits and loaded models, memory pressure, and a warning if Docker Desktop will not start at sign-in |
| `make vision-local` | Switch new analyses to the local model (`gemma4:31b` by default) |
| `make vision-anthropic` | Switch new analyses back to Anthropic |
| `make vision-local CHECK=1` / `make vision-anthropic CHECK=1` | Dry run: every check and validation, nothing changed |

The `make` targets call `scripts/vision.sh status|local|anthropic [--check]`,
which can also be run directly. A switch:

1. **Checks the image.** On the first switch to local, when the running image
   predates local vision, it uses `hermes-home:ollama-candidate` and keeps the
   previous image tagged `hermes-home:pre-ollama`.
2. **Checks the provider.** For local: Ollama is reachable, the model is
   installed and reports vision, and a warning if Ollama keeps fewer than two
   models loaded. For Anthropic: the API key is present (never displayed).
3. **Validates the new settings.** Writes a new `.env` to a private file,
   changing only the vision and timing keys — every other line, secrets
   included, is copied untouched and never printed — and runs `check-config`
   on it in a throwaway container with no database. For local, it also
   confirms the container can reach the model.
4. **Backs up the database** (`scripts/backup.sh`).
5. **Switches.** Saves the current `.env` as
   `~/hermes-home-backups/env.before-<provider>-<timestamp>` (mode 600),
   installs the new one, recreates the container, and waits up to 90 s for
   readiness.
6. **Reverts on failure.** If the service is not ready, the saved `.env` is
   restored and the container recreated again; the command exits with an
   error.

The `.env` editing in step 3 is `scripts/set_env_keys.py SOURCE DEST KEY=VALUE...`,
a standard-library-only helper that writes a new 0600 file and prints only the
values it set.

Overrides: `LOCAL_MODEL=qwen3.6:35b-a3b make vision-local` selects another
installed vision model; `ANTHROPIC_MODEL=...` does the same for Anthropic;
`BACKUP_DIR=...` moves where `.env` copies are kept.

A switch is not proof that the new model is in use. Trigger a camera, then run
`make vision-status`: the newest analysis must show the new provider, model
and prompt version. Hermes's `home_get_event` shows the same thing per event.

### Vision diagnostics

`hermes-home vision` runs the real provider code against the configured
provider without touching the database or writing a file:

| Command | Does |
|---|---|
| `hermes-home vision check` | Metadata only: Ollama's version, whether the model is installed, its capabilities, and whether `OLLAMA_THINK` suits it. Exits non-zero if the model cannot analyze images. |
| `hermes-home vision probe` | **One live analysis** of a generated 320×240 test frame (sky over ground; no household content), printing status, latency, tokens and the observation. |
| `... probe --image PATH` | Analyze a PNG or JPEG you choose instead. |
| `... probe --camera KEY` | Use that camera's prompt, including its `vision_context`. Never fetches its images. |
| `... probe --allow-billed` | Required to probe Anthropic, which bills per call. |

To probe a provider before switching to it, run the CLI in a throwaway
container with the settings passed explicitly — the exact commands are in
[docs/operations.md](docs/operations.md#local-vision-ollama). A probe proves
the plumbing (the model accepted the image and the schema, and the reply
validated), not accuracy.

## Development

```bash
.venv/bin/python -m pytest tests/ -q      # 464 tests, no network, no credentials
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format src/ tests/
```

The suite runs the real Alembic migration chain rather than `create_all`, so a
broken migration fails the tests instead of surfacing at deploy time. The Ollama
tests drive the real provider over a mocked HTTP transport: they pin down the
exact request and every way a reply is rejected, but cannot show that a real
model accepts it — that is what `hermes-home vision probe` is for.

## Documentation

| Document | Covers |
|---|---|
| [architecture.md](docs/architecture.md) | layers, data flow, why the queue is SQLite |
| [operations.md](docs/operations.md) | start/stop/upgrade/rollback/backup/restore, what starts after a reboot, local vision setup, switching, measurements and evaluation |
| [threat-model.md](docs/threat-model.md) | assets, controls, and stated limitations |
| [spatial-model.md](docs/spatial-model.md) | zones, coverage, adding a camera |
| [camera-health.md](docs/camera-health.md) | camera health, historical coverage, why unknown is not false |
| [event-pipeline.md](docs/event-pipeline.md) | detecting Home Assistant events that never arrived |
| [mcp-tools.md](docs/mcp-tools.md) | the tool contract Hermes depends on |
| [home-assistant-setup.md](docs/home-assistant-setup.md) | automation and entity wiring |
| [camera-event-images.md](docs/camera-event-images.md) | measured Eufy timing and resolution |
| [sensors.md](docs/sensors.md) | thermometers and the thermostat: syncing, ranges, unknown spans, setting it safely |

Notable tests: the end-to-end pipeline (`test_ingest_pipeline.py`), crash
recovery and exactly-once semantics (`test_worker_recovery.py`), webhook
authentication (`test_webhook_api.py`), the Ollama request contract and
rejection rules (`test_ollama_provider.py`), Ollama through the real worker and
retry path (`test_ollama_pipeline.py`), and the provider-switch scripts
(`test_vision_scripts.py`).

## MCP

Hermes reaches the semantic history through twelve deterministic tools:
`home_recent_events`, `home_search_events`, `home_get_event`, `home_list_zones`,
`home_describe_home`, `home_summarize_activity`, `home_list_cameras`,
`home_coverage`, `home_list_sensors`, `home_sensor_history`,
`home_thermostat_status`, `home_set_thermostat`. None of them calls a language
model, and none is a passthrough to Home Assistant. `home_set_thermostat` is the
only one that changes anything. It reaches only the configured thermostat,
within its configured limits, and records every request. See
[docs/mcp-tools.md](docs/mcp-tools.md).

```bash
hermes mcp add hermes-home --url http://localhost:8099/mcp/
hermes mcp test hermes-home
```

## Deployment

Runs as a container, restarting on failure and whenever the Docker engine
starts. The database lives on the host at `./data/`, outside the disposable
container filesystem. `make help` lists every operational command; see
[docs/operations.md](docs/operations.md) and [docs/threat-model.md](docs/threat-model.md).

### After a reboot

Nothing to run, once someone logs in:

1. Docker Desktop starts (Settings → General → *Start Docker Desktop when you
   sign in* must be on), and the `hermes-home` container starts with it.
2. Ollama starts from its own login item.
3. The `local.ollama.limits` LaunchAgent re-applies Ollama's loaded-model
   limits, restarting Ollama once if it came up without them.

Then `make vision-status` confirms all of it. Docker Desktop and the
LaunchAgents run inside a user session, so an unattended reboot waits for a
login. If Ollama does not come up while the local model is selected,
hermes-home still accepts and stores every event; their analyses are marked
failed until Ollama returns — never sent to Anthropic instead.

Port 8099 is published on exactly two addresses — `127.0.0.1` for Hermes and the
LAN address for Home Assistant — never `0.0.0.0`, and never to the internet.

## Security

- Published on two explicit addresses only; no public exposure, no port forwarding.
- The webhook is authenticated with a shared secret compared in constant time.
- Secrets come from the environment only, are never logged, never included in
  exception messages, and never returned in a response.
- Request bodies are size-limited; headers are never persisted (they carry the
  secret).
- The only write to a device is the thermostat. Home Assistant is called only
  for `climate.*` entities configured as thermostats, and only through
  `set_temperature`, `set_hvac_mode` or `set_preset_mode`. Setpoints must be
  inside the configured limits, and every request is audited in
  `thermostat_changes`.
- Do not expose this service to the internet.

## Roadmap

- [x] Front Door event analysis, end to end
- [x] Durable async ingestion with crash recovery
- [x] Spatial zone model and topology
- [x] Temporal query primitives
- [x] MCP tools for Hermes
- [x] Docker packaging and deployment
- [x] Multi-camera rollout (configuration, not code)
- [x] Camera health and historical coverage
- [x] Delivery reconciliation against Home Assistant
- [x] Local vision with Ollama, switchable with one command
- [x] Sensor history (ThermoPro thermometers): ranges, night ranges, honest gaps
- [x] Nest thermostat: setpoint and heating history, guarded setting
- [ ] Incident correlation across zones
- [ ] Powerwall / energy events
