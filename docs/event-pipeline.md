# Event-pipeline health: did Home Assistant's events reach us?

## Why this is a separate axis

Camera health answers *was the camera working*. It cannot answer *did its
events arrive*, and those fail independently.

On 2026-09-09 between 07:30 and 07:36 PDT, four camera triggers fired. Every
camera reported healthy. hermes-home was running and polling Home Assistant
successfully every sixty seconds. All four automations ran. All four POSTs
vanished in transit. Nothing in the system noticed, and the stored history had a
hole in it shaped exactly like a quiet morning.

Nothing about camera health was wrong, so no amount of camera-health monitoring
would ever have caught it.

## The signal, and the one that does not work

The authoritative signal is **the entity each automation actually triggers on**:

```yaml
trigger: binary_sensor.garage_right_motion_detected -> "on"
action:  rest_command.hermes_home_event
```

Every rising edge on that entity is a moment a delivery was owed. Configure it
per camera as `trigger_entity`. It is **not uniform** — this house's front door
triggers on `binary_sensor.front_door_person_detected` while the other six use
`*_motion_detected` — so it is stated explicitly and never inferred from a
naming convention.

**The event-image entity is the wrong signal**, and this was measured rather
than assumed: on 2026-09-09 at 09:03:39 `image.garage_right_event_image`
advanced with no motion, no person detection, and no automation run. Image
entities refresh for reasons that are not events. Keying reconciliation off them
would invent delivery gaps that never happened — a false alarm about missing
history is no better than a missed one.

## How reconciliation works

Every `DELIVERY_RECONCILIATION_INTERVAL_SECONDS` (default 300), per camera:

1. Read the trigger entity's recorder history over the lookback window.
   State only — never an image, so this costs the camera nothing.
2. Take rising edges into `on`. A detection holds the sensor on for seconds and
   Home Assistant records several samples, but the automation fires once, so one
   delivery is owed. Counting every sample would invent gaps.
3. Ignore edges newer than `DELIVERY_SETTLE_SECONDS` — a delivery still waiting
   out the freshness gate has not been lost.
4. For each remaining edge, look for a delivery for that camera within
   `DELIVERY_MATCH_WINDOW_SECONDS`.
5. No match → record a gap. Match → resolve any gap previously recorded there.

**Matching is against deliveries, not persisted events.** A delivery that
arrived and was rejected as a stale image *did* reach us; that is an ingest
outcome, not a transport loss, and counting it as a delivery gap would blame the
network for a camera timing problem.

Gap rows are keyed `(camera_key, ha_trigger_at)` — Home Assistant's own
timestamp — so the deliberately overlapping lookback re-examines the same
triggers without ever filing a duplicate.

## One missed event, or several?

**Several, distinguishably.** The recorder keeps a timestamp per transition, so
the morning incident is reported as three distinct garage_right losses plus one
garage_left, not as "something went wrong". That precision is only available
within the recorder's own retention; beyond it, nothing is knowable.

## Detection only — no backfill

`image_proxy` serves only the *current* bytes. Three missed events left one
surviving frame, and by detection time that frame had already been replaced by
the 09:03:39 non-event refresh. Recovering "the latest event image" would have
attached an unrelated frame to a real detection and filed it as history.

Backfill would be safe only with all of: exactly one unmatched trigger, the
image timestamp still matching the one that followed *that* trigger, and it
newer than the camera's last processed `source_state_ts`. That window expires on
any later refresh, including refreshes that are not events. It is a narrow,
fragile path guarding a rare case, and getting it wrong fabricates history —
which is the failure this entire feature exists to prevent. Reporting "3 events
missed, not recoverable" is the more useful answer.

## Pipeline status

| | |
|---|---|
| `healthy` | reconciliation running, no unmatched triggers |
| `degraded` | one or more triggers have no delivery — history is missing events |
| `unknown` | reconciliation not running, never run, or its data has gone stale |

`verification_mode` says how much that status is worth:

| | |
|---|---|
| `active` | a trigger was recently confirmed delivered end to end |
| `no_recent_trigger` | reconciliation is working and finding nothing wrong, but nothing has fired lately to exercise the path |
| `passive` | reconciliation is not running |

**A quiet camera is `healthy` / `no_recent_trigger`, never degraded and never
unknown.** Nothing needed delivering, and the mechanism that would have noticed
was working. Requiring traffic to claim health would make every quiet night
indistinguishable from an outage.

## Historical coverage

```
true   reconciliation was running throughout, and every trigger in the
       interval had a matching delivery -- including when there were none
false  at least one trigger has no delivery: events are known missing
null   reconciliation was not running, so nothing can be claimed
```

A quiet interval with reconciliation running is `true`, not unknown. But a
period before reconciliation existed is `null` — absence of gap rows is not
evidence of delivery, for the same reason absence of outage rows is not evidence
of health.

## The three dimensions, kept apart

Historical query results carry all three, because they answer different
questions and fail independently:

```json
{
  "field_of_view":           { "status": "partial", "cameras": ["backyard", "cottage"] },
  "camera_health_coverage":  { "complete": null,  "unknown_periods": [...] },
  "event_pipeline_coverage": { "complete": false, "delivery_gaps": [...] },
  "events": []
}
```

For the real incident, that reads: the cameras appear to have been working;
our observation of their health had a gap; Home Assistant recorded three
garage_right triggers; hermes-home received none of them; therefore the event
history is incomplete.

Compare with what a single merged "coverage" field could have said — it would
have had to pick one of those to report, and every choice is misleading.

## Configuration

```
DELIVERY_RECONCILIATION_ENABLED=true
DELIVERY_RECONCILIATION_INTERVAL_SECONDS=300
DELIVERY_RECONCILIATION_LOOKBACK_SECONDS=3600
DELIVERY_SETTLE_SECONDS=90
DELIVERY_MATCH_WINDOW_SECONDS=30
```

Lookback must exceed the interval so a slow or skipped pass leaves no
unexamined hole. Settle must exceed the freshness budget plus vision time, or a
delivery still in flight is reported as lost. A camera without a
`trigger_entity` is simply not reconciled — reported as unknown, never as clean.

## Limitations

- Bounded by Home Assistant's recorder retention. Beyond it, unknown.
- A trigger that fired while Home Assistant itself was down was never recorded,
  so it cannot be reconciled. Unknowable, and reported as such.
- Detection is delayed by up to one reconciliation interval plus the settle
  window. This finds losses; it does not prevent them.
- Reconciliation proves a delivery arrived, not that it produced a usable event.
  A delivery rejected for a stale image is a healthy pipeline and a missing
  observation at once; the two are reported separately on purpose.
