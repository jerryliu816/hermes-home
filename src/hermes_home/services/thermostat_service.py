"""Reading and setting a thermostat: the one place hermes-home changes a device.

Everywhere else this service is a record. The exception is deliberate and
narrow:

- only a ``climate.*`` entity named under a sensor's ``thermostat:`` block in
  ``config/sensors.yaml``;
- only ``set_temperature``, ``set_hvac_mode`` and ``set_preset_mode``;
- only setpoints inside the configured limits, and modes in the allowed list;
- every attempt, refused ones included, written to ``thermostat_changes``;
- success is what the device reports back afterwards, not what was asked for.

Checks happen before Home Assistant is called, so a refused request changes
nothing. They are written against Home Assistant's climate model, not a brand:
a setpoint while the mode is off, a single setpoint in heat_cool mode, or a
setpoint while a preset such as eco is overriding it would each be accepted by
the API and then silently ignored by the device, so each is refused here with
the reason instead.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermes_home.clients.home_assistant import EntityState, HomeAssistantClient
from hermes_home.config import SensorConfig, SensorsConfig, Settings
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc, to_display_tz
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import ThermostatChange
from hermes_home.storage.repositories import ThermostatRepository

logger = structlog.get_logger(__name__)

#: How closely a read-back setpoint must match the one asked for. Devices round
#: (Nest to half a degree Fahrenheit), so exact equality would report a real
#: change as unconfirmed.
_TOLERANCE = 0.6

#: Preset values that mean "no preset is overriding the setpoint".
_NO_PRESET = (None, "none", "None", "")


class Outcome:
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    REFUSED = "refused"
    FAILED = "failed"


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def snapshot(state: EntityState) -> dict[str, Any]:
    """The parts of a climate entity's state that matter to a reader."""
    a = state.attributes
    return {
        "mode": state.state,
        "action": a.get("hvac_action"),
        "setpoint": _num(a.get("temperature")),
        "setpoint_low": _num(a.get("target_temp_low")),
        "setpoint_high": _num(a.get("target_temp_high")),
        "current_temperature": _num(a.get("current_temperature")),
        "current_humidity": _num(a.get("current_humidity")),
        "preset": a.get("preset_mode"),
        "fan_mode": a.get("fan_mode"),
    }


class Refused(Exception):
    """A request that must not reach Home Assistant, and why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ThermostatService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        settings: Settings,
        sensors: SensorsConfig,
        ha_client: HomeAssistantClient,
        confirm_timeout_seconds: float = 15.0,
        confirm_poll_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._sensors = sensors
        self._ha = ha_client
        self._confirm_timeout = confirm_timeout_seconds
        self._confirm_poll = confirm_poll_seconds
        self._tz = settings.display_timezone

    def _local(self, value: datetime | None) -> str | None:
        return to_display_tz(value, self._tz).isoformat() if value is not None else None

    # ----------------------------------------------------------------- #
    # Resolution
    # ----------------------------------------------------------------- #

    def resolve(self, thermostat: str | None) -> tuple[list[str], str | None]:
        """Thermostat keys matching a key/name/alias (all when None), or an error."""
        found = self._sensors.thermostats()
        if not found:
            return [], "no thermostat is configured (a sensor with a thermostat: block)"
        if thermostat is None:
            return sorted(found), None
        wanted = thermostat.strip().casefold()
        for key, cfg in found.items():
            if wanted in {n.casefold() for n in (key, cfg.name, *cfg.aliases)}:
                return [key], None
        return [], (
            f"no thermostat called {thermostat!r}; configured thermostats: "
            + ", ".join(sorted(found))
        )

    def _cfg(self, key: str) -> SensorConfig:
        cfg = self._sensors.sensors[key]
        assert cfg.thermostat is not None
        return cfg

    def _limits(self, key: str) -> dict[str, Any]:
        t = self._cfg(key).thermostat
        assert t is not None
        return {
            "setpoint_min": t.setpoint.min,
            "setpoint_max": t.setpoint.max,
            "modes": list(t.modes),
            "unit": t.unit,
        }

    # ----------------------------------------------------------------- #
    # Reading
    # ----------------------------------------------------------------- #

    async def status(self, key: str) -> dict[str, Any]:
        cfg = self._cfg(key)
        assert cfg.thermostat is not None
        header = {
            "key": key,
            "name": cfg.name,
            "aliases": list(cfg.aliases),
            "zone": cfg.location,
            "entity": cfg.thermostat.entity,
            "limits": self._limits(key),
            "history_measurements": sorted(cfg.measurements),
        }
        async with session_scope(self._session_factory) as session:
            changes = await ThermostatRepository(session).recent(key)
            recent = [self._change(c) for c in changes]
        try:
            state = await self._ha.get_entity_state(cfg.thermostat.entity)
        except HomeAssistantError as exc:
            return header | {
                "status": "unknown",
                "error": exc.code,
                "now": None,
                "recent_changes": recent,
            }
        now = snapshot(state)
        available = state.state not in ("unavailable", "unknown")
        return header | {
            "status": "ok" if available else state.state,
            "now": now if available else None,
            "read_at": self._local(now_utc()),
            "device": {
                "modes": state.attributes.get("hvac_modes"),
                "presets": state.attributes.get("preset_modes"),
                "min_temp": _num(state.attributes.get("min_temp")),
                "max_temp": _num(state.attributes.get("max_temp")),
            },
            "recent_changes": recent,
        }

    def _change(self, row: ThermostatChange) -> dict[str, Any]:
        return {
            "uid": row.uid,
            "requested_at": self._local(row.requested_at),
            "request": row.request,
            "outcome": row.outcome,
            "error_code": row.error_code,
            "detail": row.detail,
            "before": row.before,
            "after": row.after,
        }

    # ----------------------------------------------------------------- #
    # Setting
    # ----------------------------------------------------------------- #

    async def set(
        self,
        key: str,
        *,
        temperature: float | None = None,
        low: float | None = None,
        high: float | None = None,
        mode: str | None = None,
        preset: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._cfg(key)
        t = cfg.thermostat
        assert t is not None
        request = {
            k: v
            for k, v in {
                "temperature": temperature,
                "target_temp_low": low,
                "target_temp_high": high,
                "hvac_mode": mode,
                "preset_mode": preset,
            }.items()
            if v is not None
        }
        requested_at = now_utc()
        before: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        outcome = Outcome.FAILED
        code: str | None = None
        detail: str | None = None
        sent = False
        try:
            if not request:
                raise Refused("nothing_to_change", "give temperature, low/high, mode or preset")
            # Unreadable means nothing is sent: a change made blind could not be
            # checked against the limits that depend on the device's state.
            state = await self._ha.get_entity_state(t.entity)
            if state.state in ("unavailable", "unknown"):
                raise Refused(
                    "thermostat_unavailable",
                    f"Home Assistant reports the thermostat {state.state}; nothing was sent",
                )
            before = snapshot(state)
            calls = self._plan(key, state, request)
            if not calls:
                # Already so. Nothing is sent, and the device's own state is the
                # confirmation.
                after, confirmed = before, True
                detail = "the thermostat was already in the requested state; nothing was sent"
            else:
                for service, data in calls:
                    sent = True
                    await self._ha.call_climate_service(service, {"entity_id": t.entity, **data})
                after, confirmed = await self._confirm(t.entity, request)
            outcome = Outcome.CONFIRMED if confirmed else Outcome.UNCONFIRMED
            if not confirmed:
                detail = (
                    "Home Assistant accepted the change, but the thermostat did not report it "
                    f"within {self._confirm_timeout:.0f} s. It may still apply; check status."
                )
        except Refused as exc:
            outcome, code, detail = Outcome.REFUSED, exc.code, exc.detail
        except HomeAssistantError as exc:
            outcome, code, detail = Outcome.FAILED, exc.code, str(exc)

        row = ThermostatChange(
            uid=new_uid(),
            sensor_key=key,
            entity_id=t.entity,
            requested_at=requested_at,
            request=request,
            outcome=outcome,
            error_code=code,
            detail=detail[:500] if detail else None,
            before=before,
            after=after,
        )
        async with session_scope(self._session_factory) as session:
            ThermostatRepository(session).add(row)
        logger.info(
            "thermostat.set",
            thermostat=key,
            request=request,
            outcome=outcome,
            error_code=code,
        )
        return {
            "key": key,
            "name": cfg.name,
            "change_uid": row.uid,
            "requested": request,
            "outcome": outcome,
            # True only when this request changed the thermostat.
            "changed": outcome == Outcome.CONFIRMED and sent,
            "error_code": code,
            "detail": detail,
            "before": before,
            "after": after,
            "limits": self._limits(key),
        }

    def _plan(
        self, key: str, state: EntityState, request: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        """The service calls for a request, or :class:`Refused` with the reason."""
        t = self._cfg(key).thermostat
        assert t is not None
        attrs = state.attributes
        temperature = request.get("temperature")
        low = request.get("target_temp_low")
        high = request.get("target_temp_high")
        mode = request.get("hvac_mode")
        preset = request.get("preset_mode")

        if mode is not None:
            if mode not in t.modes:
                raise Refused(
                    "mode_not_allowed",
                    f"mode {mode!r} is not allowed here; allowed: {', '.join(t.modes)}",
                )
            device_modes = attrs.get("hvac_modes") or []
            if device_modes and mode not in device_modes:
                raise Refused(
                    "mode_not_supported",
                    f"the thermostat does not support mode {mode!r}; it supports "
                    + ", ".join(map(str, device_modes)),
                )
        if preset is not None:
            presets = attrs.get("preset_modes") or []
            if preset not in presets:
                raise Refused(
                    "preset_not_supported",
                    f"preset {preset!r} is not one of the thermostat's presets: "
                    + (", ".join(map(str, presets)) or "none"),
                )

        target_mode = mode or state.state
        target_preset = preset if preset is not None else attrs.get("preset_mode")
        wants_setpoint = temperature is not None or low is not None or high is not None

        if wants_setpoint:
            if target_mode == "off":
                raise Refused(
                    "mode_off",
                    "the thermostat is off, so a setpoint would have no effect; "
                    "also give a mode (e.g. heat) to turn it on",
                )
            if target_preset not in _NO_PRESET:
                raise Refused(
                    "preset_active",
                    f"preset {target_preset!r} is overriding the setpoint; also give "
                    "preset='none' to clear it",
                )
        if temperature is not None:
            if low is not None or high is not None:
                raise Refused("ambiguous", "give either temperature, or low and high, not both")
            if target_mode == "heat_cool":
                raise Refused(
                    "needs_range",
                    "in heat_cool mode the thermostat holds a range; give low and high",
                )
            self._check_setpoint(key, state, temperature)
        if low is not None or high is not None:
            if low is None or high is None:
                raise Refused("needs_range", "give both low and high")
            if target_mode != "heat_cool":
                raise Refused(
                    "needs_single",
                    f"low/high apply only in heat_cool mode (the mode is {target_mode}); "
                    "give temperature, or also give mode='heat_cool'",
                )
            if low >= high:
                raise Refused("bad_range", f"low {low} must be below high {high}")
            self._check_setpoint(key, state, low)
            self._check_setpoint(key, state, high)

        calls: list[tuple[str, dict[str, Any]]] = []
        # A preset is cleared first: on some thermostats changing mode or
        # setpoint while one is active is ignored.
        if preset is not None and preset != attrs.get("preset_mode"):
            calls.append(("set_preset_mode", {"preset_mode": preset}))
        if mode is not None and mode != state.state:
            calls.append(("set_hvac_mode", {"hvac_mode": mode}))
        if temperature is not None:
            calls.append(("set_temperature", {"temperature": temperature}))
        if low is not None and high is not None:
            calls.append(("set_temperature", {"target_temp_low": low, "target_temp_high": high}))
        if temperature is not None and _close(_num(attrs.get("temperature")), temperature):
            calls = [c for c in calls if "temperature" not in c[1]]
        if low is not None and (
            _close(_num(attrs.get("target_temp_low")), low)
            and _close(_num(attrs.get("target_temp_high")), high)
        ):
            calls = [c for c in calls if "target_temp_low" not in c[1]]
        return calls

    def _check_setpoint(self, key: str, state: EntityState, value: float) -> None:
        t = self._cfg(key).thermostat
        assert t is not None
        lo, hi = t.setpoint.min, t.setpoint.max
        device_lo = _num(state.attributes.get("min_temp"))
        device_hi = _num(state.attributes.get("max_temp"))
        if device_lo is not None:
            lo = max(lo, device_lo)
        if device_hi is not None:
            hi = min(hi, device_hi)
        if not lo <= value <= hi:
            raise Refused(
                "out_of_limits",
                f"{value:g} {t.unit} is outside the allowed setpoints {lo:g}-{hi:g} {t.unit}",
            )

    async def _confirm(
        self, entity: str, request: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, bool]:
        """Read the device back until it shows the change, or time runs out."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._confirm_timeout
        last: dict[str, Any] | None = None
        while True:
            await asyncio.sleep(self._confirm_poll)
            try:
                last = snapshot(await self._ha.get_entity_state(entity))
            except HomeAssistantError:
                pass  # keep polling; an unread moment is not a verdict
            else:
                if _matches(last, request):
                    return last, True
            if loop.time() >= deadline:
                return last, False


def _close(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= _TOLERANCE


def _matches(now: dict[str, Any], request: dict[str, Any]) -> bool:
    if "hvac_mode" in request and now["mode"] != request["hvac_mode"]:
        return False
    if "preset_mode" in request and (now["preset"] or "none") != request["preset_mode"]:
        return False
    if "temperature" in request and not _close(now["setpoint"], request["temperature"]):
        return False
    if "target_temp_low" in request and not (
        _close(now["setpoint_low"], request["target_temp_low"])
        and _close(now["setpoint_high"], request["target_temp_high"])
    ):
        return False
    return True
