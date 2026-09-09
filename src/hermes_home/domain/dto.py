"""What leaves this service.

These shapes are an external contract: Hermes' prompts and config depend on the
tool names and the field names here, so renaming one means touching another
system. Two rules follow from that.

**Only ``uid`` is ever exposed.** Database row ids stay internal, so we remain
free to renumber, re-import, or re-derive without breaking a caller.

**Unknown is an explicit ``null``, never an omitted key.** A language model
reading a result will infer "zero" from an absent field. Keeping the key present
with a null value is what preserves the distinction all the way to the reader.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AnalysisView(BaseModel):
    """One vision attempt, as reported outward."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(description="ok | failed | refused | skipped")
    provider: str
    model: str | None
    prompt_version: str
    attempt: int
    observation: dict[str, Any] | None = Field(
        description="Structured scene observation; null when the analysis did not succeed."
    )
    error_code: str | None = None
    latency_ms: int | None = None


class EventView(BaseModel):
    """One observation, as reported outward."""

    model_config = ConfigDict(extra="forbid")

    uid: str
    event_type: str = Field(description='Dotted namespace, e.g. "camera.person_detected".')
    source: str
    source_entity_id: str | None
    camera: str | None = Field(description="Camera key from cameras.yaml, when known.")
    zone: str | None = Field(description="Zone key where the event occurred.")
    zone_name: str | None

    occurred_at: datetime = Field(description="When it happened, UTC.")
    occurred_at_local: str = Field(description="Same instant in the configured display timezone.")
    received_at: datetime

    tags: list[str] = Field(default_factory=list)
    summary: str | None = Field(
        default=None, description="One-line scene summary from the latest successful analysis."
    )
    analysis: AnalysisView | None = Field(
        default=None, description="Latest analysis attempt; null if never analyzed."
    )
    duplicate_count: int = Field(
        description="How many additional deliveries were suppressed as this same event."
    )
    incident_uid: str | None = None


class ZoneView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    kind: str
    adjacent_to: list[str] = Field(default_factory=list)
    relations: list[dict[str, str]] = Field(default_factory=list)
    observed_by_cameras: list[str] = Field(default_factory=list)


class CameraView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    aliases: list[str] = Field(
        default_factory=list,
        description="Other names for this camera, e.g. an older name still used in speech.",
    )
    located_in: str
    observes: list[str] = Field(default_factory=list)
    partial_coverage: list[str] = Field(
        default_factory=list,
        description="Zones this camera sees only part of.",
    )
    current_health: str | None = Field(
        default=None,
        description=(
            "Effective health right now: healthy | degraded | offline | unknown. "
            "Distinct from the fields above, which describe where the camera points "
            "regardless of whether it is working."
        ),
    )
    health_checked_at: datetime | None = Field(
        default=None, description="When health was last confirmed. Null if never monitored."
    )


class CameraHealthView(BaseModel):
    """One camera's current health, as reported outward.

    ``status`` is the *effective* status, computed at read time. A persisted
    "healthy" row is only as good as its ``checked_at``: if the monitor stopped,
    crashed or was disabled, that row would otherwise keep asserting health
    forever. So staleness is evaluated when the question is asked, not when the
    answer was written -- a dead monitor cannot mask itself by failing to write.
    ``persisted_status`` keeps the raw value visible so the difference is
    inspectable rather than merely asserted.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    located_in: str
    observes: list[str] = Field(default_factory=list)
    partial_coverage: list[str] = Field(default_factory=list)

    status: str = Field(description="healthy | degraded | offline | unknown (effective).")
    reason: str | None = Field(default=None, description="Structured reason code, or null.")
    persisted_status: str | None = Field(
        default=None,
        description="What the monitor last wrote, before staleness was applied.",
    )
    checked_at: datetime | None = Field(default=None, description="Last completed health poll.")
    last_healthy_at: datetime | None = None
    offline_since: datetime | None = None
    camera_state: str | None = Field(default=None, description="Raw Home Assistant entity state.")
    image_state: str | None = None
    last_image_update_at: datetime | None = None
    last_event_at: datetime | None = Field(
        default=None,
        description=(
            "When this camera last produced a stored event. NOT a health signal: a "
            "camera with no events for days may be perfectly healthy in a quiet week."
        ),
    )
    monitored: bool = Field(
        default=True, description="False when health monitoring is disabled for the service."
    )


class CoverageSpan(BaseModel):
    """One stretch of time that was not covered, or not verifiable."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    status: str
    reason: str | None = None
    camera: str | None = None


class CoverageView(BaseModel):
    """Whether cameras were actually working over a period.

    Entirely distinct from field of view. This says whether the equipment was
    operating; ``field_of_view`` says where it points. A zone can be fully
    covered operationally and still only partly visible, and merging the two
    into one number is how a partly-watched zone becomes an all-clear.
    """

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    complete: bool | None = Field(
        description=(
            "true = confirmed covered; false = a known gap exists; null = cannot be "
            "determined. Null is NOT 'fine' -- it means nobody was recording health "
            "then, so no claim either way is available."
        )
    )
    reason: str | None = None
    cameras_considered: list[str] = Field(default_factory=list)
    coverage_gaps: list[CoverageSpan] = Field(default_factory=list)
    unknown_periods: list[CoverageSpan] = Field(default_factory=list)
    field_of_view: dict[str, Any] | None = Field(
        default=None,
        description="Static coverage of the zone: which cameras point at it, and how fully.",
    )


class HomeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    timezone: str
    zones: list[ZoneView]
    cameras: list[CameraView]
    unobserved_zones: list[str] = Field(
        description="Zones no camera watches. Absence of events there means nothing."
    )
    partially_observed_zones: list[str] = Field(
        default_factory=list,
        description=(
            "Zones only partly covered. An absence of events is weaker evidence here "
            "than in a fully covered zone -- say so rather than reporting an all-clear."
        ),
    )


class ActivitySummary(BaseModel):
    """A deterministic tally. No prose, no inference, no model call.

    Natural-language synthesis is Hermes' job; this hands it counts and a
    timeline to reason over.
    """

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    timezone: str
    event_count: int
    by_type: dict[str, int]
    by_zone: dict[str, int]
    by_camera: dict[str, int]
    by_tag: dict[str, int]
    incident_count: int
    timeline: list[EventView]
    note: str | None = None
