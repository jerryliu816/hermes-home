"""Anthropic vision provider.

Uses the SDK's structured-output helper (``messages.parse`` with
``output_format``), so the model returns a validated :class:`SceneObservation`
rather than prose we would have to parse and hope about.

Effort is set to ``low``: this is a bounded perception task on one frame, not a
reasoning problem, and low effort keeps latency and cost sane on a stream of
doorbell events.

The API key is read from settings and never logged or included in an error
message.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import structlog

from hermes_home.domain.observations import SceneObservation
from hermes_home.vision.base import (
    RETRYABLE_CODES,
    VisionErrorCode,
    VisionRequest,
    VisionResult,
    VisionStatus,
)
from hermes_home.vision.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt

logger = structlog.get_logger(__name__)

# Per-million-token rates, used only for a rough cost estimate on each analysis
# row. Wrong-but-close beats absent when you are wondering what the camera cost
# you last month; update when pricing changes.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


class AnthropicVisionProvider:
    """Scene analysis via the Anthropic Messages API."""

    name = "anthropic"

    def __init__(self, *, api_key: str, model: str = "claude-sonnet-5") -> None:
        if not api_key:
            # A programmer/config error, not an analysis failure: crash loudly at
            # startup rather than write a "failed" row on every event.
            raise ValueError("AnthropicVisionProvider requires an API key")
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError(
                "the 'anthropic' extra is not installed; pip install 'hermes-home[anthropic]'"
            ) from exc

        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def health_check(self) -> bool:
        return True

    async def analyze(self, request: VisionRequest) -> VisionResult:
        started = time.monotonic()
        try:
            response = await self._client.messages.parse(
                model=self._model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                output_format=SceneObservation,
                output_config={"effort": "low"},
                timeout=request.timeout_seconds,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": request.media_type,
                                    "data": base64.b64encode(request.image).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": build_user_prompt(request.context)},
                        ],
                    }
                ],
            )
        except Exception as exc:  # mapped to a structured result below
            return self._failure(exc, started)

        latency_ms = int((time.monotonic() - started) * 1000)

        # A safety refusal is a terminal outcome, not an error to retry.
        if getattr(response, "stop_reason", None) == "refusal":
            return VisionResult(
                status=VisionStatus.REFUSED,
                provider=self.name,
                model=self._model,
                prompt_version=PROMPT_VERSION,
                latency_ms=latency_ms,
                error_code=VisionErrorCode.REFUSED,
                error_message="model declined to analyze this image",
                retryable=False,
                **self._usage(response),
            )

        observation = getattr(response, "parsed_output", None)
        if observation is None:
            return VisionResult(
                status=VisionStatus.FAILED,
                provider=self.name,
                model=self._model,
                prompt_version=PROMPT_VERSION,
                latency_ms=latency_ms,
                error_code=VisionErrorCode.BAD_RESPONSE,
                error_message="response contained no parsed structured output",
                retryable=False,
                **self._usage(response),
            )

        usage = self._usage(response)
        return VisionResult(
            status=VisionStatus.OK,
            observation=observation,
            provider=self.name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            cost_usd=self._estimate_cost(usage),
            **usage,
        )

    # ----------------------------------------------------------------- #

    def _failure(self, exc: Exception, started: float) -> VisionResult:
        code = _classify(exc)
        logger.warning(
            "vision.anthropic.failed",
            error_code=code.value,
            error_type=type(exc).__name__,
        )
        return VisionResult(
            status=VisionStatus.FAILED,
            provider=self.name,
            model=self._model,
            prompt_version=PROMPT_VERSION,
            latency_ms=int((time.monotonic() - started) * 1000),
            error_code=code,
            error_message=f"{type(exc).__name__}: {exc}"[:1000],
            retryable=code in RETRYABLE_CODES,
        )

    @staticmethod
    def _usage(response: Any) -> dict[str, int | None]:
        usage = getattr(response, "usage", None)
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }

    def _estimate_cost(self, usage: dict[str, int | None]) -> float | None:
        rates = _PRICING_USD_PER_MTOK.get(self._model)
        if not rates:
            return None
        input_tokens = usage.get("input_tokens") or 0
        output_tokens = usage.get("output_tokens") or 0
        return round(
            (input_tokens / 1_000_000) * rates[0] + (output_tokens / 1_000_000) * rates[1], 6
        )


def _classify(exc: Exception) -> VisionErrorCode:
    """Map an SDK exception onto our structured error vocabulary."""
    name = type(exc).__name__
    if "Timeout" in name:
        return VisionErrorCode.TIMEOUT
    if "RateLimit" in name:
        return VisionErrorCode.RATE_LIMITED
    if "Authentication" in name or "PermissionDenied" in name:
        return VisionErrorCode.AUTH
    if "InternalServer" in name or "APIConnection" in name or "Overloaded" in name:
        return VisionErrorCode.SERVER_ERROR
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status == 401 or status == 403:
            return VisionErrorCode.AUTH
        if status == 429:
            return VisionErrorCode.RATE_LIMITED
        if status >= 500:
            return VisionErrorCode.SERVER_ERROR
    return VisionErrorCode.UNEXPECTED
