# hermes-home

Turns the cameras and sensors around a house into something you can ask
questions of.

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
   vehicles, packages, activity, lighting — not prose.
4. **Throws the image away**, keeping the interpretation.
5. **Files it in place and time**: which zone, which camera, what else was
   happening nearby.
6. **Exposes it to an agent** as eight deterministic tools, so plain-language
   questions become real queries against real observations — each answer
   carrying whether the cameras behind it were actually working.

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

**Vendor-neutral.** Home Assistant is the device abstraction, so there is no
Eufy code here. Replacing every camera with another brand changes entity IDs in
a YAML file. Adding a different *kind* of device later — a Powerwall, a door
sensor — is a new event type, not a schema change, because the event table never
knew what a camera was.

**Boring to run.** One container, one SQLite file on the host, no broker, no
external services. It restarts itself, survives reboots, and backs up with
`make backup`.

## What it is not

It does not replace Home Assistant, control any device, or watch live video.
It stores no footage. It answers questions about the past; current device state
and control stay with Home Assistant, where they belong.

## How the pieces fit

Home Assistant is the device layer. Hermes is the reasoning layer. This service
is the bit in the middle that neither of them should have to be: it turns camera
events into structured, timestamped, located observations and keeps them.

```
   Physical devices  (Eufy cameras, sensors, later a Powerwall)
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
          |                 |     vision analysis                   |
          |                 |     discard the image                 |
          |                 |     persist event + analysis          |
          |                 +---------------------------------------+
          |                              |
          |                              v
          |                        SQLite  (events, analyses, zones, incidents)
          |                              |
          |  MCP tools (read-only, semantic)
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

Then follow [docs/home-assistant-setup.md](docs/home-assistant-setup.md) to add
the automation.

## Configuration

Secrets and deployment settings live in `.env` (gitignored). The non-secret
description of the house — zones, how they connect, which camera watches what —
lives in `config/home.yaml` and `config/cameras.yaml`.

Both are validated at startup. A camera pointing at a zone that does not exist
fails immediately with the offending name, rather than at 3am on a real event.

### Vision providers

| `VISION_PROVIDER` | Needs | Notes |
|---|---|---|
| `mock` (default) | nothing | Deterministic, seeded from the image hash. Fully functional. |
| `anthropic` | `ANTHROPIC_API_KEY` | Structured output via the Messages API. |

`VISION_MODEL` defaults to `claude-sonnet-5` ($2/$10 per Mtok). This fires on
every camera event, so the cost knob is deliberate and is one line:
`claude-opus-5` ($5/$25) if accuracy disappoints, `claude-haiku-4-5` ($1/$5)
once volume grows.

### Retention

| Data | Kept | Config |
|---|---|---|
| Event images | **never stored** | — |
| Raw webhook bodies | 14 days, then nulled | `DELIVERY_RAW_RETENTION_DAYS` |
| Delivery metadata (time, status, disposition, errors) | permanently | — |
| Events, analyses, tags, incidents | permanently | — |

`hermes-home db prune` runs the sweep manually; the worker also runs it hourly.

## Development

```bash
.venv/bin/python -m pytest tests/ -q      # 276 tests, no network, no credentials
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format src/ tests/
```

The suite runs the real Alembic migration chain rather than `create_all`, so a
broken migration fails the tests instead of surfacing at deploy time.

## Documentation

| Document | Covers |
|---|---|
| [architecture.md](docs/architecture.md) | layers, data flow, why the queue is SQLite |
| [operations.md](docs/operations.md) | start/stop/upgrade/rollback/backup/restore |
| [threat-model.md](docs/threat-model.md) | assets, controls, and stated limitations |
| [spatial-model.md](docs/spatial-model.md) | zones, coverage, adding a camera |
| [camera-health.md](docs/camera-health.md) | camera health, historical coverage, why unknown is not false |
| [mcp-tools.md](docs/mcp-tools.md) | the tool contract Hermes depends on |
| [home-assistant-setup.md](docs/home-assistant-setup.md) | automation and entity wiring |
| [camera-event-images.md](docs/camera-event-images.md) | measured Eufy timing and resolution |

Notable tests: the end-to-end pipeline (`test_ingest_pipeline.py`), crash
recovery and exactly-once semantics (`test_worker_recovery.py`), and webhook
authentication (`test_webhook_api.py`).

## MCP

Hermes reaches the semantic history through eight deterministic tools —
`home_recent_events`, `home_search_events`, `home_get_event`, `home_list_zones`,
`home_describe_home`, `home_summarize_activity`, `home_list_cameras`,
`home_coverage`. None of them calls a language model, and none duplicates
Hermes' existing Home Assistant tools. See [docs/mcp-tools.md](docs/mcp-tools.md).

```bash
hermes mcp add hermes-home --url http://localhost:8099/mcp/
hermes mcp test hermes-home
```

## Deployment

Runs as a container, restarting on failure and whenever the Docker engine
starts. The database lives on the host at `./data/`, outside the disposable
container filesystem. `make help` lists every operational command; see
[docs/operations.md](docs/operations.md) and [docs/threat-model.md](docs/threat-model.md).

Port 8099 is published on exactly two addresses — `127.0.0.1` for Hermes and the
LAN address for Home Assistant — never `0.0.0.0`, and never to the internet.

## Security

- Published on two explicit addresses only; no public exposure, no port forwarding.
- The webhook is authenticated with a shared secret compared in constant time.
- Secrets come from the environment only, are never logged, never included in
  exception messages, and never returned in a response.
- Request bodies are size-limited; headers are never persisted (they carry the
  secret).
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
- [ ] Incident correlation across zones
- [ ] Powerwall / energy events
