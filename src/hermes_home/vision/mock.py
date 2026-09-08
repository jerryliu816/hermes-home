"""Deterministic mock vision provider.

The service must run end-to-end with no API keys at all, so this is not a test
stub bolted on the side -- it is the default provider.

Two properties are deliberate:

* **Deterministic from the image bytes.** The same frame always yields the same
  observation, so tests are stable and a replayed delivery is comparable.
* **Some fields are null by default.** Unknown handling then sits on the normal
  code path rather than being an edge case only the real provider ever reaches.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from collections.abc import Iterable

from hermes_home.domain.observations import SceneObservation
from hermes_home.vision.base import (
    RETRYABLE_CODES,
    VisionErrorCode,
    VisionRequest,
    VisionResult,
    VisionStatus,
)
from hermes_home.vision.prompts import PROMPT_VERSION

_ACTIVITIES = ["package_delivery", "visitor_at_door", "passing_by", "no_activity"]
_LIGHTING = ["daylight", "low_light", "dark", "artificial"]


class MockVisionProvider:
    """A provider that invents plausible, stable observations without a network call.

    ``script`` lets a test drive a specific sequence of outcomes, e.g.
    ``["failed_retryable", "ok"]`` to exercise the orchestrator's single retry.
    Once exhausted, behavior falls back to the deterministic default.
    """

    name = "mock"

    def __init__(
        self,
        *,
        script: Iterable[str] | None = None,
        latency_seconds: float = 0.0,
        model: str = "mock-scene-v1",
    ) -> None:
        self._script: deque[str] = deque(script or ())
        self._latency = latency_seconds
        self._model = model

    async def health_check(self) -> bool:
        return True

    async def analyze(self, request: VisionRequest) -> VisionResult:
        if self._latency:
            await asyncio.sleep(self._latency)

        if self._script:
            outcome = self._script.popleft()
            scripted = self._scripted_result(outcome)
            if scripted is not None:
                return scripted

        return VisionResult(
            status=VisionStatus.OK,
            observation=self._observe(request),
            provider=self.name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            latency_ms=int(self._latency * 1000),
            input_tokens=len(request.image) // 1000,
            output_tokens=64,
            cost_usd=0.0,
        )

    def _scripted_result(self, outcome: str) -> VisionResult | None:
        codes = {
            "failed_retryable": VisionErrorCode.TIMEOUT,
            "failed_terminal": VisionErrorCode.BAD_RESPONSE,
            "refused": VisionErrorCode.REFUSED,
        }
        if outcome == "ok":
            return None
        if outcome not in codes:
            raise ValueError(f"unknown mock script outcome: {outcome!r}")

        code = codes[outcome]
        status = VisionStatus.REFUSED if outcome == "refused" else VisionStatus.FAILED
        return VisionResult(
            status=status,
            provider=self.name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            error_code=code,
            error_message=f"mock scripted outcome: {outcome}",
            retryable=code in RETRYABLE_CODES,
        )

    def _observe(self, request: VisionRequest) -> SceneObservation:
        """Derive a stable pseudo-observation from the image bytes."""
        digest = hashlib.sha256(request.image).digest()
        activity = _ACTIVITIES[digest[0] % len(_ACTIVITIES)]
        people = digest[1] % 3
        has_package = activity == "package_delivery" or digest[2] % 4 == 0

        tags: list[str] = []
        if people:
            tags.append("person_present")
        if has_package:
            tags.append("package_present")

        jacket = ["blue", "grey", "dark"][digest[3] % 3]
        attributes = [f"person in a {jacket} jacket"] if people else []

        return SceneObservation(
            scene_summary=(
                f"{people} person(s) visible at {request.context.camera_name}."
                if people
                else f"No people visible at {request.context.camera_name}."
            ),
            person_count=people,
            # Left null on purpose: the mock genuinely cannot tell, and this keeps
            # null-handling exercised on the default path.
            vehicle_count=None,
            animal_count=None,
            package_present=has_package,
            activity=activity,
            lighting=_LIGHTING[digest[4] % len(_LIGHTING)],
            importance="normal" if people else "low",
            overall_confidence="medium",
            notable_attributes=attributes,
            tags=tags,
            field_notes={"vehicle_count": "mock provider does not evaluate vehicles"},
        )
