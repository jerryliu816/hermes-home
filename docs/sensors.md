# Sensors

Thermometers, hygrometers, battery levels — anything Home Assistant reports as
a number. hermes-home keeps their history, places them in zones, checks them
against target ranges, and says plainly when a reading is not known.

Hermes can already read a sensor's *current* value through its own HA tools.
What it cannot get there is the past (HA's recorder purges after ten days by
default), the house's zones, the ranges that matter, or an honest account of
the hours a sensor went silent. That is what this adds.

## Not events

A thermometer reports continuously. Storing each report as an event would bury
every camera event under readings and feed the correlator things that are not
occurrences. Readings live in their own tables:

```
sensor_states       one row per change: the value held from started_at until the next row
sensor_sync_state   per measurement: how far it has been copied from HA
```

`value` is `NULL` whenever the value is not known, and `status` says why
(`unavailable`, `unknown`, `no_record`). Never 0.

## How readings arrive

Pulled, not pushed. Every `SENSOR_SYNC_INTERVAL_SECONDS` (60) the recorder asks
HA's history API for each measurement's changes since the point it last synced,
stopping 30 seconds short of now so it never runs ahead of HA's recorder
commits. Consequences:

- **No HA automation.** The existing token is all it needs.
- **Downtime loses nothing** that HA still holds. After a restart the recorder
  carries on from its sync point.
- **A new sensor arrives with history.** A measurement with no sync point is
  backfilled `SENSOR_BACKFILL_DAYS` (10) back. If HA holds nothing for part of
  that, the span is stored as `no_record` rather than left blank or filled in.
- **HA unreachable is our blindness, not the sensor's.** Nothing is written and
  the sync point does not move, so the next pass asks for the same span.

Rows older than `SENSOR_RETENTION_DAYS` (365) are pruned hourly.

## Configuration — `config/sensors.yaml`

Optional; no file means no sensors. See `config/sensors.example.yaml` for every
option. Nothing in the code names a brand, model or device: any number of
sensors, any make.

```yaml
sensors:
  gecko_enclosure:                 # permanent key, like a camera key
    name: Gecko Enclosure Thermometer
    aliases: [gecko thermometer]
    location: gecko_enclosure      # a zone in home.yaml
    measurements:
      temperature:
        entity: sensor.tp350s_cdd9_temperature
        unit: "°F"                 # optional; default is HA's unit
        range: {min: 72, max: 90}
        night: {from: "21:00", to: "07:00", min: 62, max: 80}
      battery:
        entity: sensor.tp350s_cdd9_battery
        range: {min: 20}
```

- **Keys are permanent.** They join every stored row. Rename with `name` or
  `aliases`. Pointing a measurement at a different `entity` continues the same
  history.
- **Measurement names** are any lowercase slug.
- **Units.** Temperatures in another unit than `unit` are converted (°C, °F, K).
  Any other mismatch is reported as `unit_mismatch` — unknown — rather than
  compared.
- **Ranges** may be one-sided. `night` replaces `range` between two local
  clock times in `DISPLAY_TIMEZONE`, and may cross midnight.

Adding a sensor: pair it in HA, `make sensors-discover`, paste the stub, choose
a key and zone, `make restart`. Removing one stops syncing it; its rows stay
until retention removes them.

## What a reader gets

`home_list_sensors` — the latest reading per measurement, with
`confirmed_through` (the last moment synced from HA), `status`, the range in
force now, and `in_range`. A value not confirmed within three sync intervals is
reported as `stale`, and `in_range` is then `null`.

`home_sensor_history` — one measurement over a period:

| Field | Meaning |
|---|---|
| `summary` | time-weighted mean; min and max with the time each began; `known_fraction` |
| `trend` | min / mean / max per bucket of 5 min … 1 week, chosen to fit `max_buckets` |
| `out_of_range` | each span outside the limits in force: start, end, minutes, direction, extreme, limit |
| `unknown` | each span with no known value, and why |

Every number is over **known time only**. A span outside the range is broken by
unknown time rather than bridged across it. An empty `out_of_range` alongside
`unknown` spans means "in range whenever it was known" — which is what Hermes is
told to say.

Unknown reasons: `unavailable` (HA lost the device), `unknown` (HA reported
unknown or a non-number), `no_record` (HA held nothing), `before_record`
(before this measurement's history begins), `not_yet_synced` (after the last
sync), `unit_mismatch`.

## Operations

```bash
make sensors-status      # latest value, status, range verdict and sync point per measurement
make sensors-discover    # HA sensor entities, with a stub for the unconfigured ones
make db-query SQL="SELECT sensor_key, measurement, count(*) FROM sensor_states GROUP BY 1, 2"
```

## Limitations

- HA records a state only when it changes, so a value that held for hours is one
  row. That is exactly right for a step function; it also means a sensor stuck
  on one value looks steady until HA marks it `unavailable`.
- Battery percentages from cheap BLE sensors are coarse (often 100 / 50 / 0).
