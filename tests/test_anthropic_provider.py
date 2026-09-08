"""Anthropic provider: failure mapping and refusal handling.

No network. What matters here is that every outcome becomes a structured
``VisionResult`` rather than an exception, because the pipeline persists the
failure as data and must never lose the underlying physical event.
"""

from __future__ import annotations

import pytest

from hermes_home.core.time import now_utc
from hermes_home.domain.observations import SceneObservation
from hermes_home.vision.base import (
    CameraEventContext,
    VisionErrorCode,
    VisionRequest,
    VisionStatus,
)

pytest.importorskip("anthropic")

from hermes_home.vision.anthropic import (
    AnthropicVisionProvider,
    _classify,
)


def _request() -> VisionRequest:
    return VisionRequest(
        image=b"\xff\xd8fake",
        media_type="image/jpeg",
        context=CameraEventContext(
            camera_key="front_door",
            camera_name="Front Door",
            event_type="camera.person_detected",
            occurred_at=now_utc(),
        ),
    )


class _Response:
    def __init__(self, *, parsed=None, stop_reason=None, usage=None):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.usage = usage


class _Usage:
    input_tokens = 2629
    output_tokens = 210


def _provider(monkeypatch, behavior):
    provider = AnthropicVisionProvider(api_key="test-key", model="claude-sonnet-5")

    async def parse(**kwargs):
        return behavior(kwargs)

    monkeypatch.setattr(provider._client.messages, "parse", parse)
    return provider


def test_missing_key_fails_loudly_at_construction() -> None:
    """A config error must crash at startup, not become a 'failed' row per event."""
    with pytest.raises(ValueError, match="API key"):
        AnthropicVisionProvider(api_key="", model="claude-sonnet-5")


async def test_successful_parse_becomes_an_ok_result(monkeypatch) -> None:
    observation = SceneObservation(
        scene_summary="A person walks away along the path.",
        person_count=1,
        animal_count=0,
        overall_confidence="high",
        tags=["person_present"],
    )
    provider = _provider(monkeypatch, lambda _: _Response(parsed=observation, usage=_Usage()))

    result = await provider.analyze(_request())

    assert result.status is VisionStatus.OK
    assert result.observation is not None
    assert result.observation.person_count == 1
    assert result.observation.vehicle_count is None, "unknown must survive as null"
    assert result.provider == "anthropic"
    assert result.model == "claude-sonnet-5"
    assert result.prompt_version
    assert result.input_tokens == 2629
    assert result.cost_usd and result.cost_usd > 0


async def test_the_image_is_sent_as_base64_with_its_media_type(monkeypatch) -> None:
    captured: dict = {}

    def behavior(kwargs):
        captured.update(kwargs)
        return _Response(parsed=SceneObservation(scene_summary="x"), usage=_Usage())

    await _provider(monkeypatch, behavior).analyze(_request())

    blocks = captured["messages"][0]["content"]
    image_block = next(b for b in blocks if b["type"] == "image")
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/jpeg"
    assert captured["output_format"] is SceneObservation


async def test_refusal_is_terminal_not_retryable(monkeypatch) -> None:
    provider = _provider(monkeypatch, lambda _: _Response(stop_reason="refusal", usage=_Usage()))

    result = await provider.analyze(_request())

    assert result.status is VisionStatus.REFUSED
    assert result.retryable is False
    assert result.error_code is VisionErrorCode.REFUSED


async def test_missing_structured_output_is_a_terminal_failure(monkeypatch) -> None:
    """Retrying an unparseable response usually just reproduces it."""
    provider = _provider(monkeypatch, lambda _: _Response(parsed=None, usage=_Usage()))

    result = await provider.analyze(_request())

    assert result.status is VisionStatus.FAILED
    assert result.error_code is VisionErrorCode.BAD_RESPONSE
    assert result.retryable is False


async def test_an_exception_becomes_a_result_not_a_raise(monkeypatch) -> None:
    def boom(_):
        raise RuntimeError("connection reset")

    result = await _provider(monkeypatch, boom).analyze(_request())

    assert result.status is VisionStatus.FAILED
    assert result.observation is None
    assert result.error_message


async def test_the_api_key_never_appears_in_a_failure(monkeypatch) -> None:
    def boom(_):
        raise RuntimeError("auth failed for sk-ant-supersecret")

    provider = AnthropicVisionProvider(api_key="sk-ant-supersecret", model="claude-sonnet-5")

    async def parse(**kwargs):
        return boom(kwargs)

    monkeypatch.setattr(provider._client.messages, "parse", parse)
    result = await provider.analyze(_request())

    # The provider must not add the key itself; an upstream message that echoes
    # one is out of our control, but nothing here reads self._api_key.
    assert "sk-ant-supersecret" not in repr(result.model_dump(exclude={"error_message"}))


@pytest.mark.parametrize(
    ("exc_name", "expected"),
    [
        ("APITimeoutError", VisionErrorCode.TIMEOUT),
        ("RateLimitError", VisionErrorCode.RATE_LIMITED),
        ("AuthenticationError", VisionErrorCode.AUTH),
        ("InternalServerError", VisionErrorCode.SERVER_ERROR),
        ("SomethingElse", VisionErrorCode.UNEXPECTED),
    ],
)
def test_exception_classification(exc_name: str, expected: VisionErrorCode) -> None:
    exc = type(exc_name, (Exception,), {})()
    assert _classify(exc) is expected


def test_retryable_set_matches_classification() -> None:
    from hermes_home.vision.base import RETRYABLE_CODES

    assert VisionErrorCode.TIMEOUT in RETRYABLE_CODES
    assert VisionErrorCode.RATE_LIMITED in RETRYABLE_CODES
    assert VisionErrorCode.SERVER_ERROR in RETRYABLE_CODES
    # Retrying these would only spend money reproducing the same answer.
    assert VisionErrorCode.AUTH not in RETRYABLE_CODES
    assert VisionErrorCode.BAD_RESPONSE not in RETRYABLE_CODES
    assert VisionErrorCode.REFUSED not in RETRYABLE_CODES
