"""The structured result of looking at an image.

Two design rules carry most of the weight here.

**Unknown is ``None``, never a sentinel.** SQL's three-valued logic then gives
the semantics we want for free: ``person_count > 0`` excludes unknown (we do not
know, so we must not claim a person), ``person_count = 0`` *also* excludes it
(unknown is not "nobody was there"), and AVG/SUM skip it. A ``-1`` sentinel would
silently corrupt every aggregate the first time someone forgot to filter it.

**The schema is the privacy policy.** ``extra="forbid"`` plus the absence of any
name or identity field means the model has nowhere to record "this is Jerry"
even if it tries. A schema constraint outlives a prompt instruction.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

OBSERVATION_SCHEMA_VERSION = 1

Lighting = Literal["daylight", "low_light", "dark", "artificial"]
Importance = Literal["low", "normal", "high"]
Confidence = Literal["high", "medium", "low"]


class SceneObservation(BaseModel):
    """What a vision provider reports about one frame.

    Every count and boolean is optional and defaults to ``None``, meaning
    "not determinable from this image" -- which is a different statement from
    "the analysis failed" (recorded as ``EventAnalysis.status='failed'`` with a
    null observation) and from "never analyzed" (no analysis row at all).
    """

    model_config = ConfigDict(extra="forbid")

    scene_summary: str = Field(description="One or two plain sentences describing what is visible.")

    person_count: int | None = Field(default=None, ge=0)
    vehicle_count: int | None = Field(default=None, ge=0)
    animal_count: int | None = Field(default=None, ge=0)
    package_present: bool | None = None

    activity: str | None = Field(
        default=None,
        description="Short slug for what is happening, e.g. package_delivery, passing_by.",
    )
    lighting: Lighting | None = None
    importance: Importance | None = None
    overall_confidence: Confidence | None = None

    notable_attributes: list[str] = Field(
        default_factory=list,
        description="Descriptive, non-identifying details, e.g. 'person in a blue jacket'.",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Flat labels such as person_present, package_present, vehicle_present.",
    )
    field_notes: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Why a field is null, e.g. {'person_count': 'porch column occludes left half'}."
        ),
    )

    def to_stored_json(self) -> dict[str, object]:
        """Serialize for the database.

        ``exclude_none=False`` matters: a missing key and an explicit null both
        read as NULL through ``json_extract``, so keeping every key present is
        what lets us later tell a genuine unknown from a record written by an
        older schema version.
        """
        return self.model_dump(mode="json", exclude_none=False)
