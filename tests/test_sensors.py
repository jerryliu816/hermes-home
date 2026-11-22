"""Sensor readings: config, step-function math, syncing from HA, MCP surface.

As with camera health, most of these are about what must NOT happen: a gap read
as a steady value, unknown counted as zero, a range breach invented across time
nobody recorded, or a sensor treated as special because of its brand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from hermes_home.api.deps import AppState
from hermes_home.clients.home_assistant import EntityState
from hermes_home.config import (
    CamerasConfig,
    MeasurementConfig,
    SensorsConfig,
    load_sensors_config,
    validate_sensors,
)
from hermes_home.core.errors import ConfigError, HomeAssistantError
from hermes_home.core.time import now_utc
from hermes_home.sensors import series
from hermes_home.sensors.discover import candidates, stub
from hermes_home.sensors.recorder import RECORDER_LAG, SensorRecorder, classify
from hermes_home.sensors.series import Segment, Step
from hermes_home.storage.engine import create_verification_engine, session_scope
from hermes_home.storage.models import SensorState, SensorStatus, SensorSyncState
from hermes_home.vision.mock import MockVisionProvider

#: Three sensors of different makes, one reporting Celsius and one measuring
#: something that is not temperature/humidity/battery. Nothing in the code may
#: care which is which.
SENSORS = {
    "sensors": {
        "tank": {
            "name": "Tank Thermometer",
            "aliases": ["gecko thermometer"],
            "location": "gecko_enclosure",
            "measurements": {
                "temperature": {
                    "entity": "sensor.brand_a_temperature",
                    "range": {"min": 72, "max": 90},
                    "night": {"from": "21:00", "to": "07:00", "min": 62, "max": 80},
                },
                "battery": {"entity": "sensor.brand_a_battery", "range": {"min": 20}},
            },
        },
        "garage": {
            "name": "Garage Thermometer",
            "location": "garage",
            "measurements": {
                # HA reports Celsius; ranges and results are in Fahrenheit.
                "temperature": {
                    "entity": "sensor.brand_b_temperature",
                    "unit": "°F",
                    "range": {"min": 40},
                },
            },
        },
        "cottage_air": {
            "name": "Cottage Air Monitor",
            "location": "cottage",
            "measurements": {"co2": {"entity": "sensor.brand_c_co2", "range": {"max": 1000}}},
        },
    }
}


def sensors_config(raw: dict | None = None) -> SensorsConfig:
    return SensorsConfig.model_validate(raw or SENSORS)


def at(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


class HistoryHA:
    """Home Assistant's recorder, as the REST history endpoint presents it.

    The state in force at the window start is returned stamped with the start
    time -- verified against a real HA on 2026-11-22 -- and nothing is returned
    for time before an entity's first recorded state.
    """

    def __init__(self) -> None:
        self.history: dict[str, list[tuple[datetime, str]]] = {}
        self.units: dict[str, str] = {}
        self.fail: HomeAssistantError | None = None
        self.history_calls: list[tuple[str, datetime]] = []

    def record(self, entity: str, when: datetime, state: str) -> None:
        self.history.setdefault(entity, []).append((when, state))
        self.history[entity].sort()

    async def get_entity_state(self, entity_id: str) -> EntityState:
        if self.fail:
            raise self.fail
        rows = self.history.get(entity_id, [])
        return EntityState(
            entity_id=entity_id,
            state=rows[-1][1] if rows else "unknown",
            attributes={"unit_of_measurement": self.units.get(entity_id, "°F")},
            last_changed=None,
            last_updated=None,
        )

    async def get_state_history(self, entity_id, *, start, end=None):
        if self.fail:
            raise self.fail
        self.history_calls.append((entity_id, start))
        rows = self.history.get(entity_id, [])
        end = end or now_utc()
        before = [r for r in rows if r[0] <= start]
        out = [(start, before[-1][1])] if before else []
        return out + [r for r in rows if start < r[0] <= end]

    async def list_states(self) -> list[EntityState]:
        return []

    async def aclose(self) -> None:
        return None


async def sync_all(state: AppState, ha: HistoryHA) -> None:
    await recorder(state.session_factory, state.settings, state.sensors, ha).sync_once()


def recorder(session_factory, settings, sensors, ha) -> SensorRecorder:
    return SensorRecorder(
        session_factory=session_factory, settings=settings, sensors=sensors, ha_client=ha
    )


async def stored(session_factory, key: str, name: str) -> list[SensorState]:
    async with session_scope(session_factory) as session:
        return list(
            (
                await session.scalars(
                    select(SensorState)
                    .where(SensorState.sensor_key == key, SensorState.measurement == name)
                    .order_by(SensorState.started_at)
                )
            ).all()
        )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_three_sensors_of_any_make_validate(home_config) -> None:
    validate_sensors(home_config, sensors_config(), CamerasConfig())


def test_missing_sensors_file_means_no_sensors(tmp_path) -> None:
    assert load_sensors_config(tmp_path).sensors == {}


def test_empty_sensors_file_means_no_sensors(tmp_path) -> None:
    (tmp_path / "sensors.yaml").write_text("sensors:\n")
    assert load_sensors_config(tmp_path).sensors == {}


def test_unknown_zone_is_rejected(home_config) -> None:
    raw = {
        "sensors": {
            "x": {
                "name": "X",
                "location": "attic",
                "measurements": {"temperature": {"entity": "sensor.x"}},
            }
        }
    }
    with pytest.raises(ConfigError, match="attic"):
        validate_sensors(home_config, sensors_config(raw), CamerasConfig())


def test_one_entity_configured_twice_is_rejected(home_config) -> None:
    raw = {
        "sensors": {
            "a": {
                "name": "A",
                "location": "garage",
                "measurements": {"t": {"entity": "sensor.same"}},
            },
            "b": {
                "name": "B",
                "location": "garage",
                "measurements": {"t": {"entity": "sensor.same"}},
            },
        }
    }
    with pytest.raises(ConfigError, match="configured twice"):
        validate_sensors(home_config, sensors_config(raw), CamerasConfig())


def test_sensor_key_may_not_shadow_a_camera(home_config, cameras_config) -> None:
    camera_key = next(iter(cameras_config.cameras))
    raw = {
        "sensors": {
            camera_key: {
                "name": "X",
                "location": "garage",
                "measurements": {"temperature": {"entity": "sensor.x"}},
            }
        }
    }
    with pytest.raises(ConfigError, match="also a camera key"):
        validate_sensors(home_config, sensors_config(raw), cameras_config)


@pytest.mark.parametrize(
    "measurement",
    [
        {"entity": "not_an_entity"},
        {"entity": "sensor.x", "range": {}},
        {"entity": "sensor.x", "range": {"min": 9, "max": 1}},
        {"entity": "sensor.x", "night": {"from": "21:00", "to": "07:00", "min": 1}},
        {
            "entity": "sensor.x",
            "range": {"min": 1},
            "night": {"from": "9pm", "to": "07:00", "min": 1},
        },
    ],
)
def test_malformed_measurements_are_rejected(measurement) -> None:
    with pytest.raises(ValueError):
        MeasurementConfig.model_validate(measurement)


def test_measurement_names_must_be_slugs() -> None:
    raw = {
        "sensors": {
            "x": {
                "name": "X",
                "location": "garage",
                "measurements": {"Temperature!": {"entity": "sensor.x"}},
            }
        }
    }
    with pytest.raises(ValueError, match="slug"):
        sensors_config(raw)


# --------------------------------------------------------------------------- #
# Step-function arithmetic
# --------------------------------------------------------------------------- #


def test_classify_never_turns_unknown_into_zero() -> None:
    assert classify("69.44") == (69.44, SensorStatus.OK)
    assert classify("0") == (0.0, SensorStatus.OK)
    assert classify("unavailable") == (None, SensorStatus.UNAVAILABLE)
    assert classify("unknown") == (None, SensorStatus.UNKNOWN)
    assert classify("nan") == (None, SensorStatus.UNKNOWN)
    assert classify("open") == (None, SensorStatus.UNKNOWN)


def test_every_second_of_the_window_is_accounted_for() -> None:
    steps = [Step(at(1), 70.0, "ok"), Step(at(3), None, "unavailable"), Step(at(4), 72.0, "ok")]
    segs = series.segments(steps, start=at(0), end=at(6), known_through=at(5))
    assert [(s.start.hour, s.end.hour, s.status) for s in segs] == [
        (0, 1, series.BEFORE_RECORD),
        (1, 3, "ok"),
        (3, 4, "unavailable"),
        (4, 5, "ok"),
        (5, 6, series.NOT_YET_SYNCED),
    ]


def test_a_step_before_the_window_carries_in() -> None:
    segs = series.segments([Step(at(0), 70.0, "ok")], start=at(2), end=at(4), known_through=at(4))
    assert [(s.start, s.end, s.value) for s in segs] == [(at(2), at(4), 70.0)]


def test_mean_is_time_weighted_and_skips_unknown() -> None:
    segs = [
        Segment(at(0), at(3), 60.0, "ok"),  # three hours
        Segment(at(3), at(4), 90.0, "ok"),  # one hour
        Segment(at(4), at(10), None, "unavailable"),  # must not pull anything to zero
    ]
    out = series.summarize(segs)
    assert out["mean"] == pytest.approx(67.5)
    assert (out["min"], out["min_at"]) == (60.0, at(0))
    assert (out["max"], out["max_at"]) == (90.0, at(3))
    assert out["known_fraction"] == 0.4


def test_nothing_known_gives_nulls_not_zeros() -> None:
    out = series.summarize([Segment(at(0), at(1), None, "no_record")])
    assert out["min"] is None and out["mean"] is None and out["known_fraction"] == 0.0


def test_night_range_crossing_midnight_applies_only_at_night() -> None:
    cfg = sensors_config().sensors["tank"].measurements["temperature"]
    # 66F is fine at night (62-80) and too cold by day (72-90).
    segs = [Segment(at(20), at(9, day=2), 66.0, "ok")]
    spans, total = series.out_of_range(segs, cfg, "UTC")
    assert [(s["_start"], s["_end"], s["direction"], s["limit"]) for s in spans] == [
        (at(20), at(21), "below", 72),
        (at(7, day=2), at(9, day=2), "below", 72),
    ]
    assert total == 3 * 3600


def test_out_of_range_respects_the_display_timezone() -> None:
    cfg = sensors_config().sensors["tank"].measurements["temperature"]
    # 05:00 UTC is 21:00 in Los Angeles: night has begun there.
    assert series.limits_at(cfg, at(5), "America/Los_Angeles").period == "night"
    assert series.limits_at(cfg, at(5), "UTC").period == "night"
    assert series.limits_at(cfg, at(12), "America/Los_Angeles").period == "night"
    assert series.limits_at(cfg, at(18), "America/Los_Angeles").period == "day"


def test_a_gap_splits_a_breach_rather_than_extending_it() -> None:
    cfg = MeasurementConfig.model_validate({"entity": "sensor.x", "range": {"min": 70}})
    segs = [
        Segment(at(0), at(1), 65.0, "ok"),
        Segment(at(1), at(2), None, "unavailable"),
        Segment(at(2), at(3), 64.0, "ok"),
    ]
    spans, total = series.out_of_range(segs, cfg, "UTC")
    assert len(spans) == 2 and total == 2 * 3600


def test_adjacent_breaches_merge_and_keep_the_extreme() -> None:
    cfg = MeasurementConfig.model_validate({"entity": "sensor.x", "range": {"max": 80}})
    segs = [Segment(at(0), at(1), 82.0, "ok"), Segment(at(1), at(2), 85.0, "ok")]
    [span], _ = series.out_of_range(segs, cfg, "UTC")
    assert (span["_start"], span["_end"], span["extreme"], span["_extreme_at"]) == (
        at(0),
        at(2),
        85.0,
        at(1),
    )


def test_temperature_units_convert_and_others_do_not() -> None:
    assert series.convert(20.0, "°C", "°F") == pytest.approx(68.0)
    assert series.convert(68.0, "°F", "°C") == pytest.approx(20.0)
    assert series.convert(50.0, "%", "%") == 50.0
    assert series.convert(50.0, "%", "ppm") is None


def test_buckets_use_round_widths_and_report_partial_knowledge() -> None:
    segs = [Segment(at(0), at(1), 70.0, "ok"), Segment(at(1), at(2), None, "unavailable")]
    width, out = series.buckets(segs, start=at(0), end=at(0, day=2), max_buckets=24)
    assert width == timedelta(hours=1) and len(out) == 24
    assert out[0]["mean"] == 70.0 and out[0]["known_fraction"] == 1.0
    assert out[1]["mean"] is None and out[1]["known_fraction"] == 0.0


# --------------------------------------------------------------------------- #
# Syncing from Home Assistant
# --------------------------------------------------------------------------- #


async def test_backfill_marks_time_ha_has_no_record_of(session_factory, settings) -> None:
    ha = HistoryHA()
    first = now_utc() - timedelta(hours=5)
    ha.record("sensor.brand_a_temperature", first, "74.5")
    ha.record("sensor.brand_a_temperature", first + timedelta(hours=1), "unavailable")
    ha.record("sensor.brand_a_temperature", first + timedelta(hours=2), "73.0")
    await recorder(session_factory, settings, sensors_config(), ha).sync_once()

    rows = await stored(session_factory, "tank", "temperature")
    assert [(r.status, r.value) for r in rows] == [
        (SensorStatus.NO_RECORD, None),  # backfill reached further back than HA
        (SensorStatus.OK, 74.5),
        (SensorStatus.UNAVAILABLE, None),
        (SensorStatus.OK, 73.0),
    ]
    assert rows[0].started_at < first
    assert rows[1].unit == "°F"


async def test_a_second_pass_adds_nothing_new(session_factory, settings) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(hours=2), "74.5")
    rec = recorder(session_factory, settings, sensors_config(), ha)
    await rec.sync_once()
    before = len(await stored(session_factory, "tank", "temperature"))
    await rec.sync_once()  # HA re-sends the carried-over state at the new start
    assert len(await stored(session_factory, "tank", "temperature")) == before


async def test_a_new_value_after_a_pass_is_appended(session_factory, settings) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(hours=2), "74.5")
    rec = recorder(session_factory, settings, sensors_config(), ha)
    await rec.sync_once()
    # Rewind the sync point so a change "arrives" inside the next window.
    async with session_scope(session_factory) as session:
        sync = await session.get(SensorSyncState, ("tank", "temperature"))
        sync.synced_through = now_utc() - timedelta(minutes=30)
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(minutes=10), "71.0")
    await rec.sync_once()
    rows = await stored(session_factory, "tank", "temperature")
    assert rows[-1].value == 71.0


async def test_ha_failure_writes_nothing_and_keeps_sync_point(session_factory, settings) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(hours=2), "74.5")
    rec = recorder(session_factory, settings, sensors_config(), ha)
    await rec.sync_once()
    async with session_scope(session_factory) as session:
        before = (await session.get(SensorSyncState, ("tank", "temperature"))).synced_through

    ha.fail = HomeAssistantError("ha_connection", "down")
    await rec.sync_once()
    async with session_scope(session_factory) as session:
        sync = await session.get(SensorSyncState, ("tank", "temperature"))
        assert sync.synced_through == before
        assert sync.last_error == "ha_connection"
    # HA being unreachable is our blindness, not the sensor going unavailable.
    rows = await stored(session_factory, "tank", "temperature")
    assert all(r.status != SensorStatus.UNAVAILABLE for r in rows)


async def test_adding_a_sensor_backfills_only_that_sensor(session_factory, settings) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(hours=3), "74.5")
    ha.record("sensor.brand_c_co2", now_utc() - timedelta(hours=3), "640")
    only_tank = sensors_config({"sensors": {"tank": SENSORS["sensors"]["tank"]}})
    await recorder(session_factory, settings, only_tank, ha).sync_once()

    ha.history_calls.clear()
    await recorder(session_factory, settings, sensors_config(), ha).sync_once()
    starts = dict(ha.history_calls)
    backfill_floor = now_utc() - timedelta(days=settings.sensor_backfill_days)
    assert starts["sensor.brand_c_co2"] < backfill_floor + timedelta(minutes=5)
    assert starts["sensor.brand_a_temperature"] > now_utc() - timedelta(minutes=5)
    assert (await stored(session_factory, "cottage_air", "co2"))[-1].value == 640.0


async def test_removing_a_sensor_keeps_its_history(session_factory, settings) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_c_co2", now_utc() - timedelta(hours=3), "640")
    await recorder(session_factory, settings, sensors_config(), ha).sync_once()
    without = sensors_config({"sensors": {"tank": SENSORS["sensors"]["tank"]}})
    await recorder(session_factory, settings, without, ha).sync_once()
    assert await stored(session_factory, "cottage_air", "co2")


async def test_a_renamed_entity_continues_the_same_history(session_factory, settings) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_c_co2", now_utc() - timedelta(hours=3), "640")
    await recorder(session_factory, settings, sensors_config(), ha).sync_once()
    renamed = sensors_config()
    renamed.sensors["cottage_air"].measurements["co2"].entity = "sensor.cottage_co2"
    ha.record("sensor.cottage_co2", now_utc() - timedelta(hours=3), "650")
    await recorder(session_factory, settings, renamed, ha).sync_once()
    async with session_scope(session_factory) as session:
        sync = await session.get(SensorSyncState, ("cottage_air", "co2"))
        assert sync.entity_id == "sensor.cottage_co2"
    assert [r.value for r in await stored(session_factory, "cottage_air", "co2")][-1] == 650.0


async def test_no_sensors_means_no_task(session_factory, settings) -> None:
    rec = recorder(session_factory, settings, SensorsConfig(), HistoryHA())
    await rec.start()
    assert not rec.running


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _ha_state(entity: str, state: str, device_class: str | None, unit: str | None, name: str):
    attrs = {"friendly_name": name}
    if device_class:
        attrs["device_class"] = device_class
    if unit:
        attrs["unit_of_measurement"] = unit
    return EntityState(entity, state, attrs, None, None)


def test_discovery_groups_by_device_and_skips_configured() -> None:
    states = [
        _ha_state("sensor.brand_a_temperature", "74.5", "temperature", "°F", "Brand A Temperature"),
        _ha_state(
            "sensor.tp999_ab12_temperature", "20.1", "temperature", "°C", "TP999 (AB12) Temperature"
        ),
        _ha_state("sensor.tp999_ab12_humidity", "45", "humidity", "%", "TP999 (AB12) Humidity"),
        _ha_state("sensor.power", "12", "power", "W", "Power"),
        _ha_state("binary_sensor.door", "on", "door", None, "Door"),
    ]
    found = candidates(states, sensors_config())
    assert [c.entity_id for c in found] == [
        "sensor.brand_a_temperature",
        "sensor.tp999_ab12_humidity",
        "sensor.tp999_ab12_temperature",
    ]
    assert found[0].configured_as == "tank.temperature"
    text = stub(found)
    assert "  tp999_ab12:" in text and "name: TP999 (AB12)" in text
    assert "sensor.brand_a_temperature" not in text
    assert "power" not in text
    every = candidates(states, SensorsConfig(), all_numeric=True)
    assert "sensor.power" in [c.entity_id for c in every]


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def sensor_state(session_factory, settings, home_config, cameras_config) -> AppState:
    return AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        sensors=sensors_config(),
        engine=create_verification_engine(settings.database_url),
        session_factory=session_factory,
        ha_client=HistoryHA(),
        vision=MockVisionProvider(),
    )


@pytest.fixture
async def sensor_mcp(sensor_state):
    from hermes_home.mcp.server import create_mcp_server

    return create_mcp_server(sensor_state)


async def call(server, name: str, arguments: dict | None = None) -> dict:
    result = await server.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_list_sensors_reports_value_unit_and_range(sensor_mcp, sensor_state) -> None:
    ha = HistoryHA()
    ha.units["sensor.brand_b_temperature"] = "°C"
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(hours=2), "200")
    ha.record("sensor.brand_b_temperature", now_utc() - timedelta(hours=2), "20")
    await sync_all(sensor_state, ha)

    out = await call(sensor_mcp, "home_list_sensors")
    by_key = {s["key"]: s for s in out["sensors"]}
    assert set(by_key) == {"tank", "garage", "cottage_air"}

    tank = by_key["tank"]["measurements"]["temperature"]
    assert (tank["value"], tank["status"], tank["in_range"]) == (200.0, "ok", False)
    garage = by_key["garage"]["measurements"]["temperature"]
    assert (garage["value"], garage["unit"]) == (68.0, "°F")  # 20 °C, converted
    # A measurement HA has never reported is not in range, out of range or zero.
    battery = by_key["tank"]["measurements"]["battery"]
    assert battery["value"] is None and battery["in_range"] is None
    # A plain thermometer says so, rather than leaving a reader to guess.
    assert by_key["tank"]["kind"] == "sensor"
    assert by_key["tank"]["thermostat_entity"] is None
    assert "query_context" in out


async def test_list_sensors_resolves_aliases_and_zones(sensor_mcp) -> None:
    out = await call(sensor_mcp, "home_list_sensors", {"sensor": "Gecko Thermometer"})
    assert [s["key"] for s in out["sensors"]] == ["tank"]
    out = await call(sensor_mcp, "home_list_sensors", {"zone": "garage"})
    assert [s["key"] for s in out["sensors"]] == ["garage"]
    out = await call(sensor_mcp, "home_list_sensors", {"sensor": "attic"})
    assert out["sensors"] == [] and "tank" in out["error"]


async def test_a_value_nobody_has_confirmed_lately_is_stale(sensor_mcp, sensor_state) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_a_temperature", now_utc() - timedelta(hours=2), "75")
    await sync_all(sensor_state, ha)
    async with session_scope(sensor_state.session_factory) as session:
        sync = await session.get(SensorSyncState, ("tank", "temperature"))
        sync.synced_through = now_utc() - timedelta(hours=1)
    out = await call(sensor_mcp, "home_list_sensors", {"sensor": "tank"})
    temp = out["sensors"][0]["measurements"]["temperature"]
    assert temp["status"] == "stale" and temp["in_range"] is None and temp["value"] == 75.0


async def test_history_reports_breaches_and_gaps(sensor_mcp, sensor_state) -> None:
    ha = HistoryHA()
    t0 = now_utc() - timedelta(hours=6)
    entity = "sensor.brand_c_co2"
    ha.record(entity, t0, "800")
    ha.record(entity, t0 + timedelta(hours=1), "1200")
    ha.record(entity, t0 + timedelta(hours=2), "unavailable")
    ha.record(entity, t0 + timedelta(hours=3), "900")
    await sync_all(sensor_state, ha)

    out = await call(
        sensor_mcp,
        "home_sensor_history",
        {
            "sensor": "cottage_air",
            "measurement": "co2",
            "start_time": t0.isoformat(),
            "end_time": (t0 + timedelta(hours=4)).isoformat(),
        },
    )
    [s] = out["series"]
    assert s["summary"]["max"] == 1200.0 and s["summary"]["known_fraction"] == 0.75
    [breach] = s["out_of_range"]["spans"]
    assert (breach["direction"], breach["extreme"], breach["minutes"]) == ("above", 1200.0, 60.0)
    assert [g["reason"] for g in s["unknown"]["spans"]] == ["unavailable"]
    assert s["trend"]["bucket_minutes"] == 10


async def test_history_explains_what_cannot_be_answered(sensor_mcp) -> None:
    out = await call(sensor_mcp, "home_sensor_history", {"sensor": "tank", "measurement": "co2"})
    assert "does not measure" in out["series"][0]["error"]
    out = await call(sensor_mcp, "home_sensor_history", {})
    assert "exactly one" in out["error"]


async def test_history_with_no_range_has_no_out_of_range_claim(sensor_mcp, sensor_state) -> None:
    out = await call(
        sensor_mcp, "home_sensor_history", {"sensor": "garage", "measurement": "temperature"}
    )
    assert out["series"][0]["out_of_range"] is not None  # garage has a min
    out = await call(
        sensor_mcp, "home_sensor_history", {"zone": "gecko_enclosure", "measurement": "battery"}
    )
    [s] = out["series"]
    assert s["summary"]["mean"] is None  # nothing synced: unknown, not zero
    assert out["query_context"]["note"]


async def test_describe_home_places_sensors_in_zones(sensor_mcp) -> None:
    out = await call(sensor_mcp, "home_describe_home")
    assert {s["key"]: s["located_in"] for s in out["sensors"]} == {
        "cottage_air": "cottage",
        "garage": "garage",
        "tank": "gecko_enclosure",
    }
    zones = {z["key"]: z for z in out["zones"]}
    assert zones["gecko_enclosure"]["sensors"] == ["tank"]


def test_recorder_lag_is_small() -> None:
    assert RECORDER_LAG <= timedelta(minutes=1)


async def test_a_window_reaching_into_the_future_stops_at_now(sensor_mcp, sensor_state) -> None:
    ha = HistoryHA()
    ha.record("sensor.brand_c_co2", now_utc() - timedelta(hours=3), "640")
    await sync_all(sensor_state, ha)
    out = await call(
        sensor_mcp,
        "home_sensor_history",
        {
            "sensor": "cottage_air",
            "measurement": "co2",
            "start_time": (now_utc() - timedelta(hours=2)).isoformat(),
            "end_time": (now_utc() + timedelta(hours=6)).isoformat(),
        },
    )
    assert out["query_context"]["end_clamped_to_now"] is True
    reasons = [g["reason"] for g in out["series"][0]["unknown"]["spans"]]
    # Only the recorder's lag behind now is unsynced, never six hours of future.
    assert out["series"][0]["unknown"]["minutes"] < 2 and set(reasons) <= {"not_yet_synced"}
