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
   street ── driveway ── front_walkway ── front_porch ── front_entry
                 │
              garage                              backyard  (unobserved)
```

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
