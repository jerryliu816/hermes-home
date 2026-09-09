# Camera health and historical coverage

## The distinction this exists to protect

Two questions that sound alike and are not:

| Question | Answered by | Changes when |
|---|---|---|
| Does a camera point at this zone? | `config/cameras.yaml`, `spatial.py` | you remount a camera |
| Was that camera working at 3am? | `camera_health*`, `health/` | the camera fails |

Before this existed, hermes-home modelled only the first. A camera unplugged for
a week looked exactly like a quiet zone, so "no events in the backyard overnight"
was reported the same way whether the yard was empty or the camera was dead.

The two facts are kept in separate tables, returned in separate fields, and
never merged into a single number. Merging them is how a partly-watched zone
becomes an all-clear.

## What health means

Health is **Home Assistant availability**, polled every 60 seconds. Entity state
only — no image is ever fetched for a health check.

| Status | Means |
|---|---|
| `healthy` | the camera entity answers, and its event-image entity is available |
| `degraded` | the camera answers, but the event-image entity is unavailable — a real event would produce no analyzable frame |
| `offline` | the camera entity is explicitly unavailable, or missing from Home Assistant |
| `unknown` | not determinable: Home Assistant unreachable, monitoring disabled, or the health data has gone stale |

Deliberately **not** health signals: no recent motion, no recent events, an old
event-image timestamp, a quiet zone. A camera with no motion for twelve hours is
usually a camera watching a quiet door. An `image.*` state of `unknown` is also
normal — it is the ordinary reading after a Home Assistant reload, until the
camera next fires.

If Home Assistant itself cannot be reached, cameras become `unknown`, never
`offline`. HA being down is our blindness, not the camera's failure, and the
difference is logged as a distinct `home_assistant.health_changed` event.

## Coverage is three-valued

```
true   confirmed covered
false  a known gap exists
null   cannot be determined
```

`null` is not a soft `true`. It means nobody was recording health then — before
this feature was deployed, while the service was down, or while Home Assistant
was unreachable. **Absence of an outage row is never evidence of health.**

When a period contains both a known gap and an unverifiable stretch, `complete`
is `false` and *both* lists come back. A real outage does not erase the fact that
other stretches were unverified.

## Why intervals, and what `observed_through` is for

History is stored as intervals — `(status, started_at, ended_at, observed_through)`
— rather than transitions, so "was this camera working between A and B" is an
overlap scan instead of a pairing exercise a reader can get wrong.

A row is written only when the status changes. A steady state just advances
`observed_through` on the open row: a healthy camera writes no new rows for
weeks.

`observed_through` is the load-bearing column. Without it, an open `healthy`
interval would imply healthy straight through a four-hour service outage. So:

- an interval is trusted only as far as its `observed_through`
- a poll may extend it only if less than two polling intervals have passed
- otherwise — on every monitor startup, after any crash or disabled stretch —
  the interval is closed and a new one opened **even when the status is
  identical**

Healthy before and healthy after proves nothing about the middle. The untouched
span becomes an explicit `monitoring_gap`, reported as unknown.

The one concession is a grace window on the interval still open, equal to that
same two-interval tolerance. Polling is periodic, so at any instant the last few
seconds are unconfirmed; without it, every query ending "now" would report
unknown, and a field that is always unknown is one readers learn to ignore. The
grace uses the monitor's own tolerance, so the two agree by construction: any
hole the monitor would have split on is a hole coverage reports.

## Debouncing applies to the present, never to the past

`CAMERA_HEALTH_FAILURE_THRESHOLD` stops a single dropped request from announcing
an outage. It applies to **current status only**:

| | debounced? | |
|---|---|---|
| `camera_health.status` | yes | what to tell someone asking right now |
| `camera_health_intervals` | **no** | what actually happened |

The first poll that observes `unavailable` opens an adverse interval
immediately. Debouncing history would let a genuinely observed outage vanish
from the record, which is the one thing this must never do.

The consequence is intentional: a single observed failure produces a coverage
gap about one polling interval wide, so a historical query over that minute
returns `complete: false` while current status never left `healthy`. The two can
disagree, and only the historical answer is allowed to be pessimistic.

## Current status goes stale

A persisted `healthy` row asserts health for as long as it exists. If the
monitor died an hour ago, that is a fabricated all-clear one level up from a
fabricated interval.

So the reported status is computed **at read time**: once `checked_at` is older
than three polling intervals (or monitoring is disabled), the status becomes
`unknown` with reason `health_data_stale`. `persisted_status` keeps the raw
value visible. A dead monitor cannot mask itself by failing to write.

## Zone coverage: several cameras, one place

Zone coverage is a time-slice union by sweep line. For each elementary slice the
zone is covered if **at least one** camera watching it was healthy.

So zone `complete: true` means *at least one configured camera relevant to this
zone was operational throughout every time slice.* It does **not** mean:

- that all cameras were working — a zone can be complete with one camera dead
  the entire period, and the camera-level query still reports that camera
  incomplete
- that the whole zone was visible — field of view is a separate fact, reported
  as `field_of_view`, and may still be `partial` or `none`

The backyard is the worked example: two cameras, each seeing only part of the
yard. Both healthy gives `complete: true` with `field_of_view.status: partial` —
something was watching the whole time, and it never saw all of the yard.

A zone no camera watches is **not** an operational outage. Nothing broke;
nothing was ever configured to watch it. That returns `complete: null` with
reason `not_applicable_no_cameras`, and `field_of_view.status: none` carries the
actual meaning. Reporting a health gap there would make a genuinely broken
camera indistinguishable from a wall nobody pointed one at.

## Where coverage shows up

Coverage is attached automatically rather than left to a separate call. A caller
must not be able to read `events: []` as "nothing happened" without also seeing
whether anything was watching; requiring a second tool call guarantees it is
sometimes skipped, and the time it is skipped is the time it mattered.

| Tool | Coverage attached when |
|---|---|
| `home_recent_events` | camera or zone given |
| `home_search_events` | time-bounded **and** camera or zone given |
| `home_summarize_activity` | camera or zone given (always time-bounded) |
| `home_list_cameras` | current health for every camera |
| `home_coverage` | asked directly, for one camera or one zone |
| `home_describe_home` | `current_health` per camera, beside where it points |

## Configuration

```
CAMERA_HEALTH_ENABLED=true
CAMERA_HEALTH_INTERVAL_SECONDS=60
CAMERA_HEALTH_FAILURE_THRESHOLD=2
```

Entity IDs stay in `cameras.yaml`. There is no retention setting: intervals
accrue only on real status changes, so a pruner would have nothing to do, and
deleting old intervals would destroy exactly the history this exists to answer
from.

### When entity availability is a poor proxy

Some devices — battery cameras especially — may keep their `camera.*` entity
looking available while the hardware is asleep or offline. Two optional
per-camera fields handle that without changing any code:

```yaml
backyard:
  health_entity: binary_sensor.backyard_connected
  health_healthy_states: ["on"]
```

Availability and state are independent signals, and both are honoured:

1. Home Assistant unreachable → `unknown`, always.
2. Entity `unavailable` or missing → `offline`, always. Never overridden by a
   state predicate.
3. No `health_healthy_states` → availability-only, the default and exactly the
   behaviour that existed before these fields.
4. Predicate set, state `unknown` → `unknown`. HA's own not-determinable marker
   is not evidence of disconnection.
5. Predicate set, state matches → healthy; otherwise `offline` with reason
   `health_entity_unhealthy_state`.

Comparison is exact after `strip()` and `casefold()`. There is no truthiness
table — `"true"` is not `"on"` unless you configure it as such.

**Verify both axes on real hardware before configuring this.** With the camera
up and again with it offline, record whether the candidate entity becomes
`unavailable` *and* what its state reads. Those move independently, and which
one moves decides the configuration. Guessing produces a camera that reports
healthy while disconnected — worse than not monitoring it at all.

## Failure behaviour

The monitor runs as its own asyncio task, separate from the ingest worker. Any
exception in a poll — malformed HA data included — is logged and retried on the
next interval; it can never take down the service, block webhook acceptance, or
affect vision processing.

`/ready` checks only that the monitor task is alive, and only when enabled. It
never consults Home Assistant reachability or a camera's status: an unplugged
camera would otherwise make hermes-home unready and drive a container restart
loop over exactly the condition it is designed to keep running through.

## Limitations

- Health is availability, not "the camera was recording". A camera that is
  online but obscured, misaimed, or with motion detection switched off reads as
  healthy.
- Whether HA availability tracks a physically offline battery camera is
  hardware-specific and must be verified per deployment. See above.
- A gap starts at the poll that observed it, not the moment the camera actually
  failed — unknowable at this cadence. Gaps are accurate to
  ±`CAMERA_HEALTH_INTERVAL_SECONDS`.
- Coverage before this feature was deployed is `unknown` for every camera,
  permanently and by design.
- `degraded` counts as a gap. Conservative; the reason code keeps it
  distinguishable from a true `offline`.
- Renaming a camera key orphans its health history. Guarded by documentation,
  not by the schema.
- Ingestion is not wired into health. Repeated stale-image rejections are
  visible in `event_deliveries` but do not currently mark a camera degraded;
  routine polling is the sole authority.
