# Architecture

## The boundary

Four layers, each owning one thing. The value of the design is in the lines
between them.

| Layer | Owns | Does not |
|---|---|---|
| **Home Assistant** | device connectivity, entity state, deterministic automation | interpret anything |
| **Hermes' HA tools** | *current* state, direct device interaction | remember |
| **hermes-home** | persistent semantic memory, history, spatial topology, incidents, query primitives | reason in natural language |
| **Hermes** | reasoning, synthesis, prose | talk to devices directly |

hermes-home holds no vendor code. If every Eufy camera were replaced tomorrow,
what changes is entity IDs in `config/cameras.yaml`.

Symmetrically, no natural-language reasoning lives here. Every MCP tool is
deterministic; none calls a language model. The one place a model is used is
turning a *picture* into structured fields, which is perception, not reasoning.

## Data flow

```
  Eufy camera ─ detection
        │
        ▼
  Home Assistant ── automation ── rest_command (metadata only, no image bytes)
        │
        ▼  POST /api/v1/events/home-assistant
  ┌─────────────────────────────────────────────────────────────┐
  │ hermes-home                                                  │
  │                                                              │
  │  webhook: authenticate → validate → COMMIT delivery → 202    │
  │                              │  (~5 ms; HA never waits)      │
  │                              ▼                               │
  │  worker (asyncio task, same process)                         │
  │    claim (atomic UPDATE with lease)                          │
  │      → freshness gate: wait for the still to actually arrive │
  │      → fetch image from HA                                   │
  │      → content hash; duplicate? stop before paying for vision│
  │      → vision provider → structured observation              │
  │      → DISCARD the image                                     │
  │      → persist event + analysis + tags                       │
  │      → correlate into an incident                            │
  │                              │                               │
  │                              ▼                               │
  │                     SQLite (WAL, on the host)                │
  │                              │                               │
  │  MCP server (Streamable HTTP, /mcp) ◄── services ────────────┤
  └──────────────────────────────┼───────────────────────────────┘
                                 ▼
                          Hermes Agent
```

## Why the webhook returns before doing the work

Home Assistant must never depend on this service. If the vision provider is
down, the machine is busy, or hermes-home is restarting, the automation still
completes in milliseconds and the house keeps working.

Durability comes before the acknowledgement: the delivery row is committed
*then* 202 is returned, so an accepted event survives a crash a millisecond
later. Measured: **~5 ms**, and still ~4 ms with Home Assistant unreachable.

## Why SQLite is the queue

For one home this is a handful of events a day. A broker would add an
operational dependency and buy nothing.

Claiming is a single atomic statement:

```sql
UPDATE event_deliveries SET status='processing', attempts=attempts+1,
       lease_expires_at=:lease
WHERE id = (SELECT id FROM event_deliveries
            WHERE (status='pending'    AND next_attempt_at <= :now)
               OR (status='processing' AND lease_expires_at < :now)
            ORDER BY received_at LIMIT 1)
RETURNING *;
```

Two properties fall out of that one query: two workers cannot take the same
delivery, and a delivery abandoned by a crashed worker becomes claimable again
when its lease expires. Crash recovery is not a separate mechanism — there is no
reaper to write, or to forget to write.

**Exactly-once is not the queue's job.** It comes from a `UNIQUE` constraint on
`events.delivery_key`. A replayed or redelivered job cannot insert a second
event no matter how the queue behaves. Verified by killing the service mid-job
and restarting: the delivery completed on attempt 2, one event.

## The event model

An event is an **immutable, typed, timestamped observation**. Everything
expensive, fallible, or opinionated lives in a different table.

```
event_deliveries   what arrived, and the durable work queue   (mutable)
events             what happened                              (immutable)
event_tags         cross-type query surface
event_analyses     what we inferred, one row per attempt
incidents          what several events mean together          (derived, disposable)
zones / zone_edges / entity_zones                             (from config)
```

`events` is deliberately small and knows nothing about cameras. Adding Powerwall
energy events later is a new Pydantic payload model and a new `event_type`
string — **no migration**, because nothing in the table ever knew what a camera
was. `source_entity_id` + `payload` JSON + `payload_schema_version` + a type
registry carry the variation; a `camera_entity_id` column would have ended that.

Analyses are a separate table so a retry adds a row rather than destroying the
failure before it. That is what makes "mark the analysis failed but keep the
event" real rather than aspirational.

Incidents are derived and disposable, tagged with the `correlator` that produced
them, so improving the rule means recomputing rather than migrating.

## Three ideas worth keeping

**Unknown is `NULL`, never zero.** SQL's three-valued logic then does the right
thing for free: `person_count > 0` excludes unknown, and so does
`person_count = 0`. A `-1` sentinel would corrupt every aggregate the first time
someone forgot to filter it. Four distinct states, four representations: known
zero is `0`; not determinable is `null`; analysis failed is a row with
`status='failed'`; never analyzed is no row at all.

**Time is UTC and tz-aware everywhere.** Enforced by a `UtcDateTime` type that
*raises* on naive input, not by convention. SQLite has no datetime type and
SQLAlchemy will happily round-trip a naive string; one ingest path writing local
time would leave a table nobody can later disentangle. Local time exists only at
the presentation boundary.

**Duplicate and related are different concepts.** The same delivery twice is a
duplicate — suppressed, recorded as a pointer in `event_deliveries`, never a row
in `events`. Several observations of one occurrence are *related* — that is an
incident. Conflating them either loses data or poisons every query with a
`WHERE NOT is_duplicate` someone will forget.

## Module layout

```
api/        HTTP transport: webhook, health, readiness
mcp/        MCP transport: server + tool definitions
services/   read-side logic, shared by both transports so they cannot drift
ingest/     pipeline, worker, dedupe, freshness, correlation, retention
storage/    models, engine, repositories, migrations
clients/    outbound adapters (home_assistant; a future tesla.py is a peer)
vision/     provider protocol, mock, anthropic, prompts
domain/     payloads, observations, DTOs, event-type registry
spatial.py  zones and queries
core/       time, ids, errors
```

Dependency rule: `api`/`mcp` → `services` → `ingest`/`storage`/`clients`/`vision`
→ `domain`/`core`. Never upward, never sideways between the two transports.

## Extension points

**Another camera brand.** Entity IDs in `cameras.yaml`, plus possibly
`event_image_strategy`. No code.

**Another event domain** (Powerwall, sensors, security). A payload model and a
registry entry. No migration. Every query, the retention job, the correlator and
the MCP tools keep working, because none of them knows what a camera is.

**Another vision provider.** Implement `VisionProvider` — three methods — and add
one value to the enum. Ingest and storage are untouched.

**Higher-resolution images.** `fetch_event_image()` is the only place that
decides which bytes represent an event. See `camera-event-images.md`.
