"""The vision provider seam.

Failure is a **return value, not an exception**. The failure *is* data we are
required to persist -- status, error code, whether it is worth retrying, latency,
partial token usage -- so routing it through an exception would mean either
losing that detail or building a rich exception that is a return value with extra
steps and a try/except at every call site. It also lets the mock provider
simulate every failure mode, and makes the retry policy a pure function of
``(status, retryable)`` that is testable without raising anything.

Providers may still raise, but only for programmer errors: missing credentials,
an unsupported media type, bad configuration. Those should crash loudly at
startup rather than become a persisted "failed" row.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from hermes_home.domain.observations import SceneObservation


class VisionTask(StrEnum):
    SCENE_DESCRIPTION = "scene_description"


class VisionStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    #: The model declined to analyze the image. Terminal -- never retried.
    REFUSED = "refused"


class VisionErrorCode(StrEnum):
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    AUTH = "auth"
    BAD_RESPONSE = "bad_response"
    REFUSED = "refused"
    UNEXPECTED = "unexpected"


#: Retrying these can plausibly succeed; retrying anything else just reproduces
#: the same failure and spends money doing it.
RETRYABLE_CODES = frozenset(
    {VisionErrorCode.TIMEOUT, VisionErrorCode.RATE_LIMITED, VisionErrorCode.SERVER_ERROR}
)


class CameraEventContext(BaseModel):
    """What the provider is told about the event, to ground the description."""

    model_config = ConfigDict(extra="forbid")

    camera_key: str
    camera_name: str
    zone_name: str | None = None
    observes: list[str] = Field(default_factory=list)
    event_type: str
    occurred_at: datetime


class VisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    image: bytes
    media_type: str = "image/jpeg"
    task: VisionTask = VisionTask.SCENE_DESCRIPTION
    context: CameraEventContext
    timeout_seconds: float = 30.0


class VisionResult(BaseModel):
    """Outcome of one analysis attempt. Persisted verbatim as an EventAnalysis row."""

    model_config = ConfigDict(extra="forbid")

    status: VisionStatus
    observation: SceneObservation | None = None
    provider: str
    model: str | None = None
    prompt_version: str
    latency_ms: int = 0

    error_code: VisionErrorCode | None = None
    error_message: str | None = None
    retryable: bool = False

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def ok(self) -> bool:
        return self.status is VisionStatus.OK and self.observation is not None


@runtime_checkable
class VisionProvider(Protocol):
    """Anything that can look at an image and describe it structurally."""

    name: str

    async def analyze(self, request: VisionRequest) -> VisionResult: ...

    async def health_check(self) -> bool: ...
