"""Vision layer: structured output, unknown handling, and failure as data."""

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
from hermes_home.vision.mock import MockVisionProvider
from tests.conftest import TINY_IMAGE


def _request(image: bytes = TINY_IMAGE) -> VisionRequest:
    return VisionRequest(
        image=image,
        media_type="image/jpeg",
        context=CameraEventContext(
            camera_key="front_door",
            camera_name="Front Door",
            zone_name="Front Entry",
            observes=["front_porch"],
            event_type="camera.person_detected",
            occurred_at=now_utc(),
        ),
    )


async def test_mock_returns_structured_observation() -> None:
    result = await MockVisionProvider().analyze(_request())

    assert result.status is VisionStatus.OK
    assert isinstance(result.observation, SceneObservation)
    assert result.observation.scene_summary
    assert result.prompt_version


async def test_mock_is_deterministic_for_the_same_image() -> None:
    first = await MockVisionProvider().analyze(_request())
    second = await MockVisionProvider().analyze(_request())
    assert first.observation == second.observation


async def test_unknown_fields_stay_none_not_zero() -> None:
    """The whole unknown design fails if null quietly becomes 0."""
    result = await MockVisionProvider().analyze(_request())
    assert result.observation is not None
    assert result.observation.vehicle_count is None
    assert result.observation.vehicle_count != 0


async def test_stored_json_keeps_null_keys_present() -> None:
    """A missing key and an explicit null both read as NULL via json_extract,
    so every key must be present to tell a real unknown from an old schema."""
    result = await MockVisionProvider().analyze(_request())
    stored = result.observation.to_stored_json()

    assert "vehicle_count" in stored
    assert stored["vehicle_count"] is None
    assert "animal_count" in stored


def test_observation_forbids_identity_fields() -> None:
    """extra='forbid' means the model has nowhere to record who someone is."""
    with pytest.raises(ValueError):
        SceneObservation(scene_summary="a person", person_identity="Jerry")


def test_observation_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        SceneObservation(scene_summary="x", person_count=-1)


async def test_scripted_retryable_failure_is_data_not_exception() -> None:
    provider = MockVisionProvider(script=["failed_retryable"])
    result = await provider.analyze(_request())

    assert result.status is VisionStatus.FAILED
    assert result.retryable is True
    assert result.error_code is VisionErrorCode.TIMEOUT
    assert result.observation is None


async def test_scripted_terminal_failure_is_not_retryable() -> None:
    result = await MockVisionProvider(script=["failed_terminal"]).analyze(_request())
    assert result.status is VisionStatus.FAILED
    assert result.retryable is False


async def test_refusal_is_terminal() -> None:
    """A safety refusal is an outcome, not a transient error to retry."""
    result = await MockVisionProvider(script=["refused"]).analyze(_request())
    assert result.status is VisionStatus.REFUSED
    assert result.retryable is False


async def test_script_falls_back_to_normal_behavior_when_exhausted() -> None:
    provider = MockVisionProvider(script=["failed_retryable"])
    assert (await provider.analyze(_request())).status is VisionStatus.FAILED
    assert (await provider.analyze(_request())).status is VisionStatus.OK
