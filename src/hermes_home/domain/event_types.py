"""Registry mapping an event type string to its payload model and version.

Roughly thirty lines, and they are the entire justification for storing payloads
as JSON rather than in per-type tables. Without them, six months from now there
are three payload shapes behind one event type, no way to tell them apart, and
every reader grows a defensive chain of ``.get()`` calls.

Adding Powerwall support later is an entry in this table plus a Pydantic model.
No migration, because nothing in the ``events`` table knows what a camera is.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from hermes_home.domain.payloads import CameraEventPayload

# Dotted namespaces. Prefix-queryable with LIKE 'camera.%'.
CAMERA_MOTION = "camera.motion"
CAMERA_PERSON_DETECTED = "camera.person_detected"
CAMERA_VEHICLE_DETECTED = "camera.vehicle_detected"
CAMERA_ANIMAL_DETECTED = "camera.animal_detected"
CAMERA_PACKAGE_DETECTED = "camera.package_detected"
CAMERA_DOORBELL_PRESSED = "camera.doorbell_pressed"


@dataclass(frozen=True)
class EventTypeSpec:
    event_type: str
    payload_model: type[BaseModel]
    payload_schema_version: int
    #: Whether ingestion should fetch and analyze an image for this type.
    needs_image: bool


_REGISTRY: dict[str, EventTypeSpec] = {
    spec.event_type: spec
    for spec in (
        EventTypeSpec(CAMERA_MOTION, CameraEventPayload, 1, needs_image=True),
        EventTypeSpec(CAMERA_PERSON_DETECTED, CameraEventPayload, 1, needs_image=True),
        EventTypeSpec(CAMERA_VEHICLE_DETECTED, CameraEventPayload, 1, needs_image=True),
        EventTypeSpec(CAMERA_ANIMAL_DETECTED, CameraEventPayload, 1, needs_image=True),
        EventTypeSpec(CAMERA_PACKAGE_DETECTED, CameraEventPayload, 1, needs_image=True),
        EventTypeSpec(CAMERA_DOORBELL_PRESSED, CameraEventPayload, 1, needs_image=True),
    )
}


class UnknownEventTypeError(ValueError):
    """The webhook named an event type we do not have a payload contract for."""


def get_spec(event_type: str) -> EventTypeSpec:
    try:
        return _REGISTRY[event_type]
    except KeyError as exc:
        raise UnknownEventTypeError(
            f"unknown event_type {event_type!r}; known types: {sorted(_REGISTRY)}"
        ) from exc


def is_known(event_type: str) -> bool:
    return event_type in _REGISTRY


def known_event_types() -> list[str]:
    return sorted(_REGISTRY)
