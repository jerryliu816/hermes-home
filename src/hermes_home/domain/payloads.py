"""Typed payloads for each event type.

The payload column is JSON, and this is what stops that from rotting: every
event type has a Pydantic model and a version, validated at the write boundary.
Enforcement moves from DDL to Python, which is in fact stronger than what SQLite
offers -- SQLite does not enforce column types at all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CameraEventPayload(BaseModel):
    """A camera reported something. Vendor-neutral by construction."""

    model_config = ConfigDict(extra="allow")

    camera: str = Field(description="Camera key as declared in cameras.yaml.")
    trigger_entity: str | None = None
    event_image_entity: str | None = None
    camera_entity: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SensorEventPayload(BaseModel):
    """Reserved for a future generic sensor event. Not produced in v1."""

    model_config = ConfigDict(extra="allow")

    entity_id: str
    state: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
