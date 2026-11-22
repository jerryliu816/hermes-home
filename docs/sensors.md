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
- **Attributes.** `attribute: temperature` reads that attribute of the entity
  instead of its state, for example a thermostat's setpoint. HA gives no unit
  for an attribute, so `unit` (or `states`) is required. Two measurements may
  read different attributes of one entity.
- **Text states.** `states: {heating: 1, idle: 0, off: 0}` turns text into
  numbers. The time-weighted mean of a 0/1 measurement is the share of time it
  was 1. A state not in the map is stored as `unknown`, never as 0. A bare
  `off` in YAML parses as a boolean; it is read back as `"off"`.

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

## Thermostats

A thermostat is a sensor with a `thermostat:` block. In this house it is the
Hallway Google Nest, `climate.hallway_hallway` through HA's Nest integration.
Nothing in the code names Nest. Any HA `climate.*` entity works.

```yaml
  hallway:
    name: Hallway Thermostat
    aliases: [thermostat, nest, hallway]
    location: hallway
    thermostat:
      entity: climate.hallway_hallway
      setpoint: {min: 60, max: 76}     # what Hermes may set, inside the device's 50-90
      modes: [heat, cool, heat_cool, off]   # leave out `off` to forbid turning it off
    measurements:
      temperature: {entity: sensor.hallway_hallway_temperature, unit: "°F"}
      humidity:    {entity: sensor.hallway_hallway_humidity}
      setpoint:    {entity: climate.hallway_hallway, attribute: temperature, unit: "°F"}
      heating:     {entity: climate.hallway_hallway, attribute: hvac_action,
                    states: {heating: 1, idle: 0, off: 0, cooling: 0}}
```

**History** works like any other sensor: `home_sensor_history(sensor="hallway",
measurement=...)`. The `setpoint` reads as unknown while the thermostat is off
or holding a `heat_cool` range, because it then has no single setpoint. Over a
period, `heating`'s `summary.mean` is the share of the time the heat ran.

**Which temperature is the thermostat's.** In `home_list_sensors` and
`home_sensor_history`, every sensor carries `kind`: `thermostat` (with
`thermostat_entity`) or `sensor`. The Nest's own temperature is the `hallway`
entry; the ThermoPros are `sensor`.

**Reading now.** `home_thermostat_status` reads live from HA: mode, action,
setpoint or low/high, current temperature and humidity, preset, the limits,
and the last five requested changes.

**Setting.** `home_set_thermostat` takes any of `temperature`, `low` + `high`,
`mode` and `preset`. Before anything is sent, it reads the thermostat and
refuses, with a code and a reason:

| Code | When |
|---|---|
| `out_of_limits` | a setpoint outside `setpoint` (or the device's own min/max, if tighter) |
| `mode_not_allowed` / `mode_not_supported` | a mode not in `modes`, or not offered by the device |
| `preset_not_supported` | a preset the device does not offer |
| `mode_off` | a setpoint while the mode is (or would be) off: it would do nothing |
| `preset_active` | a setpoint while a preset such as eco overrides it; add `preset="none"` |
| `needs_range` / `needs_single` | a single setpoint in `heat_cool`, or low/high outside it |
| `bad_range` | low not below high |
| `nothing_to_change`, `thermostat_unavailable` | nothing asked for; HA reports the device unavailable |

When several changes are requested, they are applied in this order: preset,
then mode, then setpoint. The thermostat is then read back for up to 15
seconds:

- `confirmed` means it reports the change. A request that matches the current
  state is also `confirmed`, and nothing is sent.
- `unconfirmed` means HA accepted the call but the device has not reported it
  yet. Nest goes through Google's cloud, so this can happen.
- `refused` and `failed` (HA unreachable) mean nothing changed.

`after` is always what the device says, not what was asked for.

Every attempt is a row in `thermostat_changes` (request, outcome, before,
after). It is kept indefinitely: it is small, and it is the answer to "who
turned the heat down?".

The HA client can POST only to `climate.set_temperature`, `set_hvac_mode` and
`set_preset_mode`, and only for a `climate.*` entity. Any other call is
refused in the client itself.

## Operations

```bash
make sensors-status      # latest value, status, range verdict and sync point per measurement
make sensors-discover    # HA sensor entities, with a stub for the unconfigured ones
                         # (and a thermostat stub for each unconfigured climate.* entity)
make thermostat-status   # live thermostat state, limits, recent changes
make db-query SQL="SELECT requested_at, request, outcome, error_code FROM thermostat_changes ORDER BY id DESC LIMIT 10"
make db-query SQL="SELECT sensor_key, measurement, count(*) FROM sensor_states GROUP BY 1, 2"
```

## Limitations

- HA records a state only when it changes, so a value that held for hours is one
  row. That is exactly right for a step function; it also means a sensor stuck
  on one value looks steady until HA marks it `unavailable`.
- Battery percentages from cheap BLE sensors are coarse (often 100 / 50 / 0).
