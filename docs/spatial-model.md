# Spatial model

The house is represented as a small labelled graph, not as a picture. That is
what lets an agent answer "was anyone near the front door" without anyone having
written code about front doors.

## What exists in V1

| Concept | Table | Source of truth |
|---|---|---|
| Zone | `zones` | `config/home.yaml` |
| Relationship between zones | `zone_edges` | `config/home.yaml` |
| Which entity is where, and what it can see | `entity_zones` | `config/cameras.yaml` |

Config is authoritative; the tables are the copy SQL joins and MCP tools can
see. Seeding runs on every startup and **converges**: a camera removed from the
config has its rows deleted, rather than lingering and resolving events to a
zone nothing watches any more.

## Zones

```yaml
zones:
  front_entry:
    name: Front Entry
    kind: threshold        # interior | exterior | threshold | structure
```

`kind` is a coarse classification. `attributes` is a free-form map reserved for
optional geometry (see below) and carries nothing in V1.

## Relationships

```yaml
relationships:
  - from: driveway
    to: front_walkway
    relation: adjacent      # adjacent | leads_to | overlooks
  - from: front_walkway
    to: front_porch
    relation: leads_to
```

Declared once and stored in both directions when `bidirectional` (the default),
so either endpoint can be queried. The current property graph:

```
                        street
                          │
   garage ─ garage_entry ─ driveway ── front_walkway ── front_porch ── front_entry
      │    [garage_left]      │              │                          [front_door]
      │    [garage_right]     │              │
      │                       │              │
   garage_side_door ── left_walkway     right_walkway
                    [left_walkway]      [right_walkway]
                           │                  │
                           └──── backyard ────┘
                              [backyard]  │
                                  │       └── rear_entry
                               cottage
                              [cottage]
```

Cameras in brackets sit *at* a zone and look outward from it.

`garage_left` and `garage_right` are both mounted on the garage's front-facing
wall, so they observe the **driveway** and not the garage interior — which is
why `garage` stays in `unobserved_zones`. Two cameras in one zone is deliberate:
one person crossing the driveway trips both, and that is a single occurrence, so
their events correlate into one incident while remaining two distinct events.

The two shed cameras are the opposite case. Both are on the same shed, but they
face away from each other, so they hold **separate zones** (`backyard` and
`cottage`). Overlapping hardware does not merge their histories.

**Edges are recorded but deliberately not traversed.** There is no path-finding,
no plausible-transition scoring, no trajectory estimation. The zone *schema* is
first-class in V1; a zone *engine* is not. The data is there so a future
correlator can use adjacency without a migration.

## Two distinct relationships, one entity

A camera is **located in** exactly one zone but **observes** several. Collapsing
those makes "which cameras can see the driveway" unanswerable, so
`entity_zones` has a composite primary key over `(entity_id, zone_id, role)`:

| role | meaning | used for |
|---|---|---|
| `located_in` | the entity physically sits here | placing an incoming event |
| `observes` | the entity can see into here | coverage queries |

```yaml
cameras:
  front_door:
    location: front_entry     # -> located_in
    observes:                 # -> observes
      - front_porch
      - front_walkway
```

Cross-references are validated at startup: a camera pointing at a zone that does
not exist fails immediately, naming the typo, rather than at 3am on a real event.

## Coverage here is about field of view, not about working cameras

Everything in this document describes where cameras **point** — a static fact
that changes when you remount one. Whether a camera was actually **working**
during some period is a separate axis, recorded separately, and documented in
[camera-health.md](camera-health.md). The two are never merged: a zone can be
fully covered by a camera that has been offline all week.

One consequence for camera keys: once health history exists, the key a camera is
filed under in `cameras.yaml` is a permanent identifier. Renaming it orphans
that camera's recorded history. Change `name`, or add an `aliases` entry.

## Coverage is three-valued, not two

A zone is `full`, `partial` or `none`. The middle case matters: the backyard
camera sits on the shed and sees the half of the yard nearest the house, so
"the backyard is covered" is true and misleading at the same time — the same
over-claim as calling an unwatched zone quiet, one level subtler.

Declare it on the camera:

```yaml
backyard:
  location: backyard
  observes: [backyard, rear_entry]
  partial_coverage: [backyard]      # sees only part of this one
```

Startup rejects a `partial_coverage` entry naming a zone the camera does not
cover at all, and full coverage by any other camera wins — adding a camera can
never make the reported coverage more pessimistic.

**Two partial views do not add up to a full one.** The shed carries two cameras:
the south one looks toward the house, the north one toward the cottage, and a
strip of the yard is in neither field of view. Both declare
`partial_coverage: [backyard]`, so the zone stays `partial` no matter how many
cameras point at it. Treating two partial views as full would produce a
confident all-clear over exactly the strip nothing watches.

`home_describe_home` reports `partially_observed_zones` alongside
`unobserved_zones`, and every zone-filtered query result carries a
`field_of_view` block. The second one matters more in practice: an agent asked
"did anything happen in the backyard" calls a query tool and never thinks to ask
about coverage separately, so a caveat that lives only in `describe_home` goes
unread.

`field_of_view` is one of three independent blocks on a query result, and they
answer different questions: where cameras point, whether they were working
(`camera_health_coverage`), and whether their events reached us
(`event_pipeline_coverage`). See [camera-health.md](camera-health.md) and
[event-pipeline.md](event-pipeline.md).

## Coverage, and why it is reported

`home_describe_home` returns `unobserved_zones` — zones no camera watches. This
exists so an agent can distinguish *nothing was recorded* from *nothing
happened*. Asked about the backyard, Hermes correctly answered:

> The absence of events is not evidence that nothing happened; there is
> currently no backyard observation source to detect it.

That distinction is a property of the data model, not of the prompt.

## Queries

Deterministic set lookups, no inference:

| Function | Answers |
|---|---|
| `resolve_zone_id(entity)` | where did this event happen |
| `zone_by_key` / `all_zones` | zone lookup |
| `adjacent_zone_keys(zone)` | what is next to this |
| `zone_relations(zone)` | ...and by what kind of relation |
| `entities_observing(zone)` | which entities can see here |
| `cameras_observing(zone)` | which cameras, by config key |
| `zones_covered_by(cameras)` | everything watched at all |

## Deliberately absent

No pathfinding, camera field-of-view geometry, trajectory estimation,
probabilistic transition models, cross-camera identity matching, facial
recognition, or biometric identity. Correlation groups events by *same zone
within a time window* and nothing more.

## Adding geometry later

The semantic topology above works with no coordinates, which is the point — it
is useful before any map exists. When a property map is available, coordinates
go in each zone's `attributes` with no schema change:

```yaml
zones:
  front_entry:
    name: Front Entry
    kind: threshold
    attributes:
      polygon: [[0.48, 0.12], [0.58, 0.12], [0.58, 0.22], [0.48, 0.22]]

cameras:
  front_door:
    attributes:
      position: {x: 0.52, y: 0.18}
      orientation_degrees: 180
      fov_degrees: 120
```

Nothing reads these yet. Geometry is additive: the semantic layer keeps working
without it, and adding it does not invalidate anything already stored.

## Adding a camera

Configuration only:

1. Add the zones it watches to `home.yaml`, with relationships.
2. Add the camera to `cameras.yaml` with its entity IDs, `location`, `observes`.
3. `make restart` — seeding converges the tables.
4. Add a Home Assistant automation for its trigger entity.

No code, no migration.
