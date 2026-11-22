"""Find sensor entities in Home Assistant that ``sensors.yaml`` does not use yet.

Read-only: it prints a stub to paste, and never writes configuration. Choosing
a permanent key and a zone is a decision for a person, not a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hermes_home.clients.home_assistant import EntityState
from hermes_home.config import SensorsConfig

#: Device classes offered by default. ``--all-numeric`` widens it.
DEFAULT_CLASSES = ("temperature", "humidity", "battery")


@dataclass(frozen=True)
class Candidate:
    entity_id: str
    measurement: str
    device: str
    device_name: str
    state: str
    unit: str | None
    configured_as: str | None


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug if slug and slug[0].isalpha() else f"s_{slug}"


def candidates(
    states: list[EntityState], sensors: SensorsConfig, *, all_numeric: bool = False
) -> list[Candidate]:
    """Sensor entities worth offering, grouped by the device they appear to share."""
    configured = {m.entity: f"{key}.{name}" for key, name, m in sensors.measurement_entities()}
    found: list[Candidate] = []
    for state in states:
        if not state.entity_id.startswith("sensor."):
            continue
        device_class = state.attributes.get("device_class")
        unit = state.attributes.get("unit_of_measurement")
        if device_class in DEFAULT_CLASSES:
            measurement = str(device_class)
        elif all_numeric and unit is not None and _is_number(state.state):
            measurement = str(device_class) if device_class else "value"
        else:
            continue
        object_id = state.entity_id.split(".", 1)[1]
        # HA names a device's entities "<device> <measurement>", so stripping
        # the measurement groups a thermometer's entities back together.
        device = re.sub(rf"_{re.escape(measurement)}$", "", object_id)
        friendly = str(state.attributes.get("friendly_name") or object_id)
        device_name = re.sub(rf"\s+{re.escape(measurement)}$", "", friendly, flags=re.I)
        found.append(
            Candidate(
                entity_id=state.entity_id,
                measurement=_slug(measurement),
                device=_slug(device),
                device_name=device_name,
                state=state.state,
                unit=str(unit) if unit is not None else None,
                configured_as=configured.get(state.entity_id),
            )
        )
    return sorted(found, key=lambda c: (c.device, c.measurement))


def stub(found: list[Candidate]) -> str:
    """YAML for the unconfigured candidates, to paste under ``sensors:``."""
    lines: list[str] = []
    devices: dict[str, list[Candidate]] = {}
    for c in found:
        if c.configured_as is None:
            devices.setdefault(c.device, []).append(c)
    for device, members in devices.items():
        lines += [
            f"  {device}:                # permanent key -- choose it now",
            f"    name: {members[0].device_name}",
            "    location: CHANGE_ME     # a zone key from config/home.yaml",
            "    measurements:",
        ]
        used: set[str] = set()
        for c in members:
            name = c.measurement if c.measurement not in used else _slug(c.entity_id)
            used.add(name)
            reading = f"{c.state} {c.unit}" if c.unit else c.state
            lines += [f"      {name}:", f"        entity: {c.entity_id}   # now {reading}"]
    return "\n".join(lines)


def thermostat_stub(states: list[EntityState], sensors: SensorsConfig) -> str:
    """YAML for ``climate.*`` entities no sensor sets yet: limits and history.

    The setpoint limits printed are the device's own; narrow them to what the
    house should allow before pasting.
    """
    configured = {s.thermostat.entity for s in sensors.thermostats().values() if s.thermostat}
    lines: list[str] = []
    for state in sorted(states, key=lambda s: s.entity_id):
        if not state.entity_id.startswith("climate.") or state.entity_id in configured:
            continue
        a = state.attributes
        key = _slug(state.entity_id.split(".", 1)[1])
        modes = [m for m in a.get("hvac_modes") or [] if m in ("heat", "cool", "heat_cool", "off")]
        lo, hi = a.get("min_temp", 60), a.get("max_temp", 80)
        lines += [
            f"  {key}:                # permanent key -- choose it now",
            f"    name: {a.get('friendly_name') or key} Thermostat",
            "    location: CHANGE_ME     # a zone key from config/home.yaml",
            "    thermostat:",
            f"      entity: {state.entity_id}   # now {state.state}, set to {a.get('temperature')}",
            f"      setpoint: {{min: {lo}, max: {hi}}}   # device limits; narrow them",
            f"      modes: [{', '.join(modes)}]",
            "    measurements:",
            "      setpoint:",
            f"        entity: {state.entity_id}",
            "        attribute: temperature",
            '        unit: "°F"            # the thermostat\'s unit',
            "      heating:",
            f"        entity: {state.entity_id}",
            "        attribute: hvac_action",
            "        states: {heating: 1, idle: 0, off: 0, cooling: 0}",
        ]
    return "\n".join(lines)
