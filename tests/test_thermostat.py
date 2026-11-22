"""Thermostats: recording setpoint and heating as history, and setting one.

Setting is the one place this service changes a device, so most of these are
about what must NOT happen: a request outside the limits reaching Home
Assistant, a change the device would silently ignore being reported as done, an
accepted call being reported as confirmed before the device shows it, or an
attempt going unrecorded.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hermes_home.api.deps import AppState
from hermes_home.clients.home_assistant import EntityState, HomeAssistantClient
from hermes_home.config import MeasurementConfig, SensorsConfig, validate_sensors
from hermes_home.core.errors import ConfigError, HomeAssistantError
from hermes_home.core.time import now_utc
from hermes_home.sensors.discover import thermostat_stub
from hermes_home.sensors.recorder import SensorRecorder, classify
from hermes_home.services.thermostat_service import ThermostatService
from hermes_home.storage.engine import create_verification_engine, session_scope
from hermes_home.storage.models import SensorState, SensorStatus, ThermostatChange
from hermes_home.vision.mock import MockVisionProvider

ENTITY = "climate.living_room"

THERMOSTAT = {
    "sensors": {
        "living": {
            "name": "Living Room Thermostat",
            "aliases": ["thermostat"],
            "location": "cottage",
            "thermostat": {
                "entity": ENTITY,
                "setpoint": {"min": 60, "max": 76},
                "modes": ["heat", "cool", "heat_cool", "off"],
            },
            "measurements": {
                "temperature": {"entity": "sensor.living_room_temperature", "unit": "°F"},
                "setpoint": {"entity": ENTITY, "attribute": "temperature", "unit": "°F"},
                "heating": {
                    "entity": ENTITY,
                    "attribute": "hvac_action",
                    "states": {"heating": 1, "idle": 0, "off": 0, "cooling": 0},
                },
            },
        }
    }
}


def config(raw: dict | None = None) -> SensorsConfig:
    return SensorsConfig.model_validate(raw or THERMOSTAT)


class ClimateHA:
    """A thermostat behind Home Assistant: state, attribute history, services."""

    def __init__(self, **attrs: Any) -> None:
        self.mode = attrs.pop("mode", "heat")
        self.attrs: dict[str, Any] = {
            "hvac_modes": ["heat", "cool", "heat_cool", "off"],
            "preset_modes": ["none", "eco"],
            "min_temp": 50,
            "max_temp": 90,
            "temperature": 65,
            "target_temp_low": None,
            "target_temp_high": None,
            "current_temperature": 65,
            "current_humidity": 54.0,
            "hvac_action": "idle",
            "preset_mode": "none",
            "fan_mode": "off",
        } | attrs
        self.calls: list[tuple[str, dict[str, Any]]] = []
        #: When False the device accepts calls but never reports the change.
        self.applies = True
        self.fail_reads = False
        self.attribute_history: dict[str, list] = {}

    async def get_entity_state(self, entity_id: str) -> EntityState:
        if self.fail_reads:
            raise HomeAssistantError("ha_timeout", "timed out", retryable=True)
        return EntityState(
            entity_id=entity_id,
            state=self.mode,
            attributes=dict(self.attrs),
            last_changed=None,
            last_updated=None,
        )

    async def call_climate_service(self, service: str, data: dict[str, Any]) -> None:
        self.calls.append((service, data))
        if not self.applies:
            return
        if service == "set_hvac_mode":
            self.mode = data["hvac_mode"]
        elif service == "set_preset_mode":
            self.attrs["preset_mode"] = data["preset_mode"]
        elif service == "set_temperature":
            for k in ("temperature", "target_temp_low", "target_temp_high"):
                if k in data:
                    self.attrs[k] = data[k]

    async def get_attribute_history(self, entity_id, attribute, *, start, end=None):
        return [(max(at, start), v) for at, v in self.attribute_history.get(attribute, [])]

    async def get_state_history(self, entity_id, *, start, end=None):
        return [(start, "65.0")]


def service(session_factory, settings, ha, sensors=None) -> ThermostatService:
    return ThermostatService(
        session_factory=session_factory,
        settings=settings,
        sensors=sensors or config(),
        ha_client=ha,
        confirm_timeout_seconds=0.05,
        confirm_poll_seconds=0.01,
    )


async def audit(session_factory) -> list[ThermostatChange]:
    async with session_scope(session_factory) as session:
        return list(
            (await session.scalars(select(ThermostatChange).order_by(ThermostatChange.id))).all()
        )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_thermostat_config_validates(home_config, cameras_config) -> None:
    sensors = config()
    validate_sensors(home_config, sensors, cameras_config)
    assert list(sensors.thermostats()) == ["living"]


def test_two_attributes_of_one_entity_are_not_a_duplicate(home_config, cameras_config) -> None:
    validate_sensors(home_config, config(), cameras_config)  # setpoint and heating share it


def test_the_same_attribute_twice_is_a_duplicate(home_config, cameras_config) -> None:
    raw = json.loads(json.dumps(THERMOSTAT))
    raw["sensors"]["living"]["measurements"]["target"] = {
        "entity": ENTITY,
        "attribute": "temperature",
        "unit": "°F",
    }
    with pytest.raises(ConfigError, match="configured twice"):
        validate_sensors(home_config, config(raw), cameras_config)


@pytest.mark.parametrize(
    "measurement",
    [
        {"entity": ENTITY, "attribute": "temperature"},  # no unit: HA gives none
        {"entity": ENTITY, "attribute": "hvac_action", "states": {}},
    ],
)
def test_malformed_attribute_measurements_are_rejected(measurement) -> None:
    with pytest.raises(ValidationError):
        MeasurementConfig.model_validate(measurement)


@pytest.mark.parametrize(
    "thermostat",
    [
        {"entity": "switch.heater", "setpoint": {"min": 60, "max": 70}},
        {"entity": ENTITY, "setpoint": {"min": 75, "max": 70}},
        {"entity": ENTITY, "setpoint": {"min": 60, "max": 70}, "modes": ["turbo"]},
    ],
)
def test_malformed_thermostats_are_rejected(thermostat) -> None:
    raw = json.loads(json.dumps(THERMOSTAT))
    raw["sensors"]["living"]["thermostat"] = thermostat
    with pytest.raises(ValidationError):
        config(raw)


def test_a_bare_yaml_off_still_means_off(tmp_path) -> None:
    from hermes_home.config import load_sensors_config

    (tmp_path / "sensors.yaml").write_text(
        f"""
sensors:
  living:
    name: Living
    location: cottage
    thermostat:
      entity: {ENTITY}
      setpoint: {{min: 60, max: 76}}
      modes: [heat, off]
    measurements:
      heating:
        entity: {ENTITY}
        attribute: hvac_action
        states: {{heating: 1, off: 0}}
"""
    )
    sensors = load_sensors_config(tmp_path)
    living = sensors.sensors["living"]
    assert living.thermostat.modes == ["heat", "off"]
    assert living.measurements["heating"].states == {"heating": 1, "off": 0}


# --------------------------------------------------------------------------- #
# Recording the setpoint and the heating
# --------------------------------------------------------------------------- #


def test_mapped_states_are_numbers_and_unmapped_ones_unknown() -> None:
    states = {"heating": 1, "idle": 0, "off": 0}
    assert classify("heating", states) == (1.0, SensorStatus.OK)
    assert classify("off", states) == (0.0, SensorStatus.OK)
    assert classify("fan", states) == (None, SensorStatus.UNKNOWN)  # never 0
    assert classify("unavailable", states) == (None, SensorStatus.UNAVAILABLE)


async def test_attributes_are_recorded_as_measurements(session_factory, settings) -> None:
    ha = ClimateHA()
    t0 = now_utc() - timedelta(hours=3)
    ha.attribute_history = {
        "temperature": [(t0, "65"), (t0 + timedelta(hours=1), "unknown")],
        "hvac_action": [(t0, "idle"), (t0 + timedelta(minutes=30), "heating")],
    }
    await SensorRecorder(
        session_factory=session_factory, settings=settings, sensors=config(), ha_client=ha
    ).sync_once()

    async with session_scope(session_factory) as session:
        rows = (
            await session.scalars(
                select(SensorState)
                .where(SensorState.sensor_key == "living")
                .order_by(SensorState.measurement, SensorState.started_at)
            )
        ).all()
    by = {}
    for r in rows:
        by.setdefault(r.measurement, []).append((r.status, r.value, r.unit))
    # Backfill reaches past HA's first record: no_record, then the values.
    assert by["setpoint"][1:] == [
        (SensorStatus.OK, 65.0, "°F"),
        (SensorStatus.UNKNOWN, None, "°F"),  # off mode: no setpoint, not 0
    ]
    assert [(s, v) for s, v, _ in by["heating"][1:]] == [
        (SensorStatus.OK, 0.0),
        (SensorStatus.OK, 1.0),
    ]


async def test_heating_history_mean_is_the_share_of_time_heating(session_factory, settings) -> None:
    from hermes_home.services.sensor_service import SensorService

    ha = ClimateHA()
    t0 = now_utc() - timedelta(hours=4)
    ha.attribute_history = {
        "temperature": [(t0, "65")],
        "hvac_action": [(t0, "idle"), (t0 + timedelta(hours=1), "heating")],
    }
    sensors = config()
    await SensorRecorder(
        session_factory=session_factory, settings=settings, sensors=sensors, ha_client=ha
    ).sync_once()
    async with session_scope(session_factory) as session:
        svc = SensorService(session, settings=settings, sensors=sensors)
        [result] = await svc.history(
            ["living"], "heating", start=t0, end=t0 + timedelta(hours=2), max_buckets=4
        )
    assert result["summary"]["mean"] == 0.5


async def test_attribute_history_parses_and_collapses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "minimal_response" not in request.url.query.decode()
        row = {"entity_id": ENTITY, "state": "heat"}
        return httpx.Response(
            200,
            json=[
                [
                    row
                    | {
                        "last_updated": "2026-11-22T10:00:00+00:00",
                        "attributes": {"temperature": 65},
                    },
                    # Another attribute changed; this one did not.
                    row
                    | {
                        "last_updated": "2026-11-22T10:05:00+00:00",
                        "attributes": {"temperature": 65},
                    },
                    row
                    | {
                        "last_updated": "2026-11-22T11:00:00+00:00",
                        "attributes": {"temperature": 68},
                    },
                    row | {"last_updated": "2026-11-22T12:00:00+00:00", "attributes": {}},
                    {
                        "entity_id": ENTITY,
                        "state": "unavailable",
                        "last_updated": "2026-11-22T13:00:00+00:00",
                        "attributes": {},
                    },
                ]
            ],
        )

    client = HomeAssistantClient(
        "http://ha.test", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    history = await client.get_attribute_history(ENTITY, "temperature", start=now_utc())
    assert [v for _, v in history] == ["65", "68", "unknown", "unavailable"]


async def test_service_calls_are_confined_to_climate() -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        "http://ha.test", "t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await client.call_climate_service("set_temperature", {"entity_id": ENTITY, "temperature": 66})
    assert sent[0].method == "POST"
    assert sent[0].url.path == "/api/services/climate/set_temperature"
    assert json.loads(sent[0].content) == {"entity_id": ENTITY, "temperature": 66}

    with pytest.raises(HomeAssistantError, match="not a permitted"):
        await client.call_climate_service("turn_on", {"entity_id": ENTITY})
    with pytest.raises(HomeAssistantError, match="climate"):
        await client.call_climate_service("set_temperature", {"entity_id": "lock.front_door"})
    assert len(sent) == 1


def test_discovery_offers_unconfigured_thermostats() -> None:
    state = EntityState(
        entity_id="climate.hallway",
        state="heat",
        attributes={"hvac_modes": ["heat", "off"], "min_temp": 50, "max_temp": 90},
        last_changed=None,
        last_updated=None,
    )
    text = thermostat_stub([state], SensorsConfig())
    assert "entity: climate.hallway" in text and "attribute: hvac_action" in text
    configured = config(
        {
            "sensors": {
                "hall": THERMOSTAT["sensors"]["living"]
                | {
                    "thermostat": THERMOSTAT["sensors"]["living"]["thermostat"]
                    | {"entity": "climate.hallway"}
                }
            }
        }
    )
    assert thermostat_stub([state], configured) == ""


# --------------------------------------------------------------------------- #
# Setting
# --------------------------------------------------------------------------- #


async def test_a_setpoint_change_is_confirmed_and_recorded(session_factory, settings) -> None:
    ha = ClimateHA()
    result = await service(session_factory, settings, ha).set("living", temperature=68)
    assert result["outcome"] == "confirmed" and result["changed"] is True
    assert result["before"]["setpoint"] == 65 and result["after"]["setpoint"] == 68
    assert ha.calls == [("set_temperature", {"entity_id": ENTITY, "temperature": 68})]
    [row] = await audit(session_factory)
    assert row.outcome == "confirmed" and row.request == {"temperature": 68}
    assert row.after["setpoint"] == 68


async def test_already_set_sends_nothing_and_confirms(session_factory, settings) -> None:
    ha = ClimateHA()
    result = await service(session_factory, settings, ha).set("living", temperature=65)
    assert result["outcome"] == "confirmed" and ha.calls == []
    assert result["changed"] is False  # confirmed as so, but nothing was changed
    assert "already" in result["detail"]


@pytest.mark.parametrize(
    ("ha_kwargs", "request_", "code"),
    [
        ({}, {"temperature": 85}, "out_of_limits"),  # inside the device's, outside ours
        ({}, {"temperature": 55}, "out_of_limits"),
        ({"max_temp": 70}, {"temperature": 72}, "out_of_limits"),  # device is tighter
        ({"mode": "off"}, {"temperature": 68}, "mode_off"),
        ({"preset_mode": "eco"}, {"temperature": 68}, "preset_active"),
        ({"mode": "heat_cool"}, {"temperature": 68}, "needs_range"),
        ({}, {"low": 64, "high": 72}, "needs_single"),
        ({"mode": "heat_cool"}, {"low": 72, "high": 64}, "bad_range"),
        ({}, {"mode": "auto"}, "mode_not_allowed"),
        ({}, {"preset": "sleep"}, "preset_not_supported"),
        ({}, {}, "nothing_to_change"),
    ],
)
async def test_refusals_never_reach_home_assistant(
    session_factory, settings, ha_kwargs, request_, code
) -> None:
    ha = ClimateHA(**ha_kwargs)
    result = await service(session_factory, settings, ha).set("living", **request_)
    assert result["outcome"] == "refused" and result["error_code"] == code, result
    assert result["changed"] is False
    assert ha.calls == []
    [row] = await audit(session_factory)
    assert row.outcome == "refused" and row.error_code == code


async def test_eco_can_be_cleared_with_the_setpoint(session_factory, settings) -> None:
    ha = ClimateHA(preset_mode="eco")
    result = await service(session_factory, settings, ha).set(
        "living", preset="none", temperature=68
    )
    assert result["outcome"] == "confirmed"
    assert [c[0] for c in ha.calls] == ["set_preset_mode", "set_temperature"]


async def test_turning_on_from_off_with_a_setpoint(session_factory, settings) -> None:
    ha = ClimateHA(mode="off", temperature=None)
    result = await service(session_factory, settings, ha).set("living", mode="heat", temperature=66)
    assert result["outcome"] == "confirmed"
    assert [c[0] for c in ha.calls] == ["set_hvac_mode", "set_temperature"]


async def test_heat_cool_range(session_factory, settings) -> None:
    ha = ClimateHA()
    result = await service(session_factory, settings, ha).set(
        "living", mode="heat_cool", low=64, high=74
    )
    assert result["outcome"] == "confirmed"
    assert ha.calls[-1] == (
        "set_temperature",
        {"entity_id": ENTITY, "target_temp_low": 64, "target_temp_high": 74},
    )


async def test_accepted_but_not_reported_is_unconfirmed(session_factory, settings) -> None:
    ha = ClimateHA()
    ha.applies = False
    result = await service(session_factory, settings, ha).set("living", temperature=68)
    assert result["outcome"] == "unconfirmed" and result["changed"] is False
    assert result["after"]["setpoint"] == 65  # what the device says, not what was asked
    [row] = await audit(session_factory)
    assert row.outcome == "unconfirmed"


async def test_an_unreadable_thermostat_is_not_changed(session_factory, settings) -> None:
    ha = ClimateHA()
    ha.fail_reads = True
    result = await service(session_factory, settings, ha).set("living", temperature=68)
    assert result["outcome"] == "failed" and result["error_code"] == "ha_timeout"
    assert ha.calls == []
    assert (await audit(session_factory))[0].outcome == "failed"


async def test_an_unavailable_thermostat_is_not_changed(session_factory, settings) -> None:
    ha = ClimateHA(mode="unavailable")
    result = await service(session_factory, settings, ha).set("living", temperature=68)
    assert result["error_code"] == "thermostat_unavailable" and ha.calls == []


# --------------------------------------------------------------------------- #
# MCP surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def thermostat_state(session_factory, settings, home_config, cameras_config) -> AppState:
    return AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        sensors=config(),
        engine=create_verification_engine(settings.database_url),
        session_factory=session_factory,
        ha_client=ClimateHA(),
        vision=MockVisionProvider(),
    )


async def call(server, name: str, arguments: dict | None = None) -> dict:
    result = await server.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_mcp_status_and_set(thermostat_state, monkeypatch) -> None:
    from hermes_home.mcp.server import create_mcp_server
    from hermes_home.services import thermostat_service

    # Keep the read-back loop fast.
    monkeypatch.setattr(
        thermostat_service.ThermostatService,
        "__init__",
        _fast_init(thermostat_service.ThermostatService.__init__),
    )
    server = create_mcp_server(thermostat_state)

    status = await call(server, "home_thermostat_status", {"thermostat": "thermostat"})
    [t] = status["thermostats"]
    assert t["key"] == "living" and t["now"]["mode"] == "heat"
    assert t["limits"]["setpoint_max"] == 76
    assert "heating" in t["history_measurements"]

    refused = await call(server, "home_set_thermostat", {"temperature": 90})
    assert refused["outcome"] == "refused"

    done = await call(server, "home_set_thermostat", {"temperature": 67})
    assert done["outcome"] == "confirmed"

    status = await call(server, "home_thermostat_status")
    assert [c["outcome"] for c in status["thermostats"][0]["recent_changes"]] == [
        "confirmed",
        "refused",
    ]

    listed = await call(server, "home_list_sensors", {"sensor": "thermostat"})
    assert listed["sensors"][0]["kind"] == "thermostat"
    assert listed["sensors"][0]["thermostat_entity"] == ENTITY

    unknown = await call(server, "home_set_thermostat", {"thermostat": "garage", "temperature": 67})
    assert unknown["outcome"] == "refused" and "no thermostat" in unknown["error"]


def _fast_init(original):
    def init(self, **kwargs):
        kwargs.setdefault("confirm_timeout_seconds", 0.05)
        kwargs.setdefault("confirm_poll_seconds", 0.01)
        original(self, **kwargs)

    return init
