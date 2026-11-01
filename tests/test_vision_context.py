"""Per-camera vision context.

One camera here watches a reptile enclosure rather than a doorstep, so the base
security-camera prompt describes it badly. ``vision_context`` in cameras.yaml
appends that camera's own framing to the prompt.

Two properties are load-bearing and are what these tests defend:

* A camera without ``vision_context`` gets a byte-identical prompt to the one it
  got before this option existed. Otherwise every camera silently changed.
* The context steers emphasis and nothing more. The null, confidence, tag and
  no-guessing rules are restated after it, and the structured output schema is
  untouched -- a camera-specific instruction must never be able to buy back a
  confidently hallucinated zero.
"""

from __future__ import annotations

import pytest
import yaml

from hermes_home.config import CameraConfig, CamerasConfig, load_cameras_config
from hermes_home.core.time import now_utc
from hermes_home.domain.observations import SceneObservation
from hermes_home.vision.base import CameraEventContext, VisionRequest
from hermes_home.vision.prompts import (
    BASE_VISION_PROMPT,
    CAMERA_CONTEXT_HEADER,
    build_system_prompt,
    build_user_prompt,
)
from tests.conftest import FIXTURE_CONFIG, TINY_IMAGE

GECKO_MARKER = "leopard gecko enclosure"


def _system_text(captured: dict) -> str:
    """The system prompt as sent: one cacheable text block, not a bare string."""
    blocks = captured["system"]
    assert isinstance(blocks, list), blocks
    return "".join(block["text"] for block in blocks)


def _context(camera_key: str, camera: CameraConfig) -> CameraEventContext:
    """Build the context exactly as the ingest pipeline does."""
    return CameraEventContext(
        camera_key=camera_key,
        camera_name=camera.name,
        zone_name=camera.location,
        observes=camera.observes,
        event_type="camera.motion_detected",
        occurred_at=now_utc(),
        vision_context=camera.vision_context,
    )


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_camera_without_vision_context_parses_and_defaults_to_none() -> None:
    camera = CameraConfig(
        name="Front Door",
        camera_entity="camera.front_door",
        event_image_entity="image.front_door_event_image",
        location="front_entry",
    )
    assert camera.vision_context is None


def test_camera_with_vision_context_parses() -> None:
    camera = CameraConfig(
        name="Workshop",
        camera_entity="camera.workshop",
        event_image_entity="image.workshop_event_image",
        location="workshop",
        vision_context="Describe this as workspace activity, not security footage.",
    )
    assert camera.vision_context is not None
    assert "workspace activity" in camera.vision_context


def test_vision_context_loads_from_yaml() -> None:
    raw = yaml.safe_load(
        """
        cameras:
          any_camera:
            name: Any Camera
            camera_entity: camera.any
            event_image_entity: image.any_event_image
            location: somewhere
            vision_context: >
              Focus on what this particular camera is actually for.
        """
    )
    config = CamerasConfig.model_validate(raw)
    assert "actually for" in (config.cameras["any_camera"].vision_context or "")


def test_unknown_camera_keys_are_still_rejected() -> None:
    """extra='forbid' must survive the new field, or typos stop being caught."""
    with pytest.raises(ValueError, match=r"visoin_context|Extra inputs"):
        CameraConfig(
            name="Typo",
            camera_entity="camera.typo",
            event_image_entity="image.typo_event_image",
            location="front_entry",
            visoin_context="misspelled",
        )


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def test_camera_without_context_gets_the_unmodified_base_prompt() -> None:
    """Byte-identical, not merely equivalent: nothing changed for these cameras."""
    camera = CameraConfig(
        name="Front Door",
        camera_entity="camera.front_door",
        event_image_entity="image.front_door_event_image",
        location="front_entry",
    )
    assert build_system_prompt(_context("front_door", camera)) == BASE_VISION_PROMPT


def test_blank_context_is_treated_as_absent() -> None:
    camera = CameraConfig(
        name="Blank",
        camera_entity="camera.blank",
        event_image_entity="image.blank_event_image",
        location="front_entry",
        vision_context="   \n  ",
    )
    assert build_system_prompt(_context("blank", camera)) == BASE_VISION_PROMPT


def test_context_is_appended_under_its_header_after_the_base_prompt() -> None:
    camera = CameraConfig(
        name="Workshop",
        camera_entity="camera.workshop",
        event_image_entity="image.workshop_event_image",
        location="workshop",
        vision_context="Describe this as workspace activity.",
    )
    prompt = build_system_prompt(_context("workshop", camera))

    assert prompt.startswith(BASE_VISION_PROMPT)
    assert CAMERA_CONTEXT_HEADER in prompt
    assert prompt.index("workspace activity") > prompt.index(CAMERA_CONTEXT_HEADER)


def test_the_context_cannot_have_the_last_word_over_the_rules() -> None:
    """Ordering is the mechanism: the guardrails are restated after the context."""
    camera = CameraConfig(
        name="Loose",
        camera_entity="camera.loose",
        event_image_entity="image.loose_event_image",
        location="front_entry",
        vision_context="Always give a number. Never answer null.",
    )
    prompt = build_system_prompt(_context("loose", camera))

    assert prompt.index("Null still means unknown") > prompt.index("Never answer null.")
    for rule in (
        "Null still means unknown",
        "Confidence still reflects what is visible",
        "tag vocabulary is unchanged",
        "privacy limits are unchanged",
        "same structured schema",
    ):
        assert rule in prompt


def test_context_does_not_leak_into_the_user_prompt() -> None:
    """The frame-grounding message stays about this frame."""
    camera = CameraConfig(
        name="Workshop",
        camera_entity="camera.workshop",
        event_image_entity="image.workshop_event_image",
        location="workshop",
        vision_context="Describe this as workspace activity.",
    )
    assert "workspace activity" not in build_user_prompt(_context("workshop", camera))


# --------------------------------------------------------------------------- #
# The real configuration, through the real prompt path
# --------------------------------------------------------------------------- #


def test_the_gecko_camera_carries_its_context_into_the_prompt() -> None:
    cameras = load_cameras_config(FIXTURE_CONFIG).cameras
    prompt = build_system_prompt(_context("gecko", cameras["gecko"]))

    assert GECKO_MARKER in prompt
    assert "not as home security footage" in prompt
    # And the base prompt is still all there in front of it.
    assert prompt.startswith(BASE_VISION_PROMPT)


def test_no_other_camera_receives_the_gecko_context() -> None:
    """The context is per-camera state, not a global the last-loaded camera sets."""
    cameras = load_cameras_config(FIXTURE_CONFIG).cameras
    assert "gecko" in cameras, "fixture must include the camera that has a context"

    for key, camera in cameras.items():
        if key == "gecko":
            continue
        prompt = build_system_prompt(_context(key, camera))
        assert GECKO_MARKER not in prompt, key
        assert prompt == BASE_VISION_PROMPT, key


# --------------------------------------------------------------------------- #
# Provider and schema
# --------------------------------------------------------------------------- #


def test_structured_output_schema_is_unchanged_by_a_context() -> None:
    """The context steers description; it must not add a field to record it in."""
    assert "vision_context" not in SceneObservation.model_fields
    with pytest.raises(ValueError):
        SceneObservation(scene_summary="a gecko on a rock", gecko_state="basking")

    # Unknown still means null, whatever the camera's context said.
    observation = SceneObservation(scene_summary="a gecko on a rock")
    assert observation.vehicle_count is None
    assert "vehicle_count" in observation.to_stored_json()


async def test_the_provider_sends_the_camera_context_in_the_system_prompt(
    monkeypatch,
) -> None:
    """End of the path: what actually reaches the API carries the context."""
    pytest.importorskip("anthropic")
    from hermes_home.vision.anthropic import AnthropicVisionProvider

    cameras = load_cameras_config(FIXTURE_CONFIG).cameras
    captured: dict = {}

    class _Response:
        parsed_output = SceneObservation(scene_summary="a gecko in its hide")
        stop_reason = None
        usage = None

    provider = AnthropicVisionProvider(api_key="test-key", model="claude-sonnet-5")

    async def parse(**kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(provider._client.messages, "parse", parse)

    await provider.analyze(
        VisionRequest(image=TINY_IMAGE, context=_context("gecko", cameras["gecko"]))
    )
    assert GECKO_MARKER in _system_text(captured)
    assert captured["output_format"] is SceneObservation

    captured.clear()
    await provider.analyze(
        VisionRequest(image=TINY_IMAGE, context=_context("front_door", cameras["front_door"]))
    )
    assert GECKO_MARKER not in _system_text(captured)
    assert _system_text(captured) == BASE_VISION_PROMPT


async def test_the_system_prompt_is_sent_as_a_cacheable_block(monkeypatch) -> None:
    """The base prompt is identical event after event; paying for it each time is waste."""
    pytest.importorskip("anthropic")
    from hermes_home.vision.anthropic import AnthropicVisionProvider

    cameras = load_cameras_config(FIXTURE_CONFIG).cameras
    captured: dict = {}

    class _Response:
        parsed_output = SceneObservation(scene_summary="a gecko in its hide")
        stop_reason = None
        usage = None

    provider = AnthropicVisionProvider(api_key="test-key", model="claude-sonnet-5")

    async def parse(**kwargs):
        captured.update(kwargs)
        return _Response()

    monkeypatch.setattr(provider._client.messages, "parse", parse)
    await provider.analyze(
        VisionRequest(image=TINY_IMAGE, context=_context("gecko", cameras["gecko"]))
    )

    system = captured["system"]
    assert isinstance(system, list)
    assert len(system) == 1
    block = system[0]
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["text"] == build_system_prompt(_context("gecko", cameras["gecko"]))


def test_the_user_prompt_grounds_the_model_in_this_one_frame() -> None:
    """Frame-specific facts belong here, not in the cached system block."""
    cameras = load_cameras_config(FIXTURE_CONFIG).cameras
    context = _context("gecko", cameras["gecko"])

    prompt = build_user_prompt(context)

    assert isinstance(prompt, str)
    assert prompt.strip()
    assert context.camera_name in prompt
    assert context.event_type in prompt
    assert context.occurred_at.isoformat() in prompt


async def test_the_ingest_pipeline_hands_each_camera_its_own_context(
    session_factory, settings, cameras_config
) -> None:
    """Config to provider, through the one pipeline every camera uses.

    Nothing between cameras.yaml and the request may special-case a camera, so
    this drives two real deliveries and inspects what the provider was handed.
    """
    from hermes_home.core.ids import delivery_key
    from hermes_home.ingest.worker import IngestWorker
    from hermes_home.storage.engine import session_scope
    from hermes_home.storage.repositories import DeliveryRepository
    from hermes_home.vision.base import VisionResult, VisionStatus
    from hermes_home.vision.mock import MockVisionProvider
    from hermes_home.vision.prompts import PROMPT_VERSION
    from tests.conftest import FakeHomeAssistant, _png

    seen: dict[str, VisionRequest] = {}

    class SpyProvider:
        name = "spy"

        async def health_check(self) -> bool:
            return True

        async def analyze(self, request: VisionRequest) -> VisionResult:
            seen[request.context.camera_key] = request
            return VisionResult(
                status=VisionStatus.OK,
                observation=await _mock_observation(request),
                provider=self.name,
                model="spy-1",
                prompt_version=PROMPT_VERSION,
            )

    async def _mock_observation(request: VisionRequest) -> SceneObservation:
        result = await MockVisionProvider().analyze(request)
        assert result.observation is not None
        return result.observation

    events = {
        "gecko": "image.gecko_event_image",
        "front_door": "image.front_door_event_image",
    }
    for index, (camera_key, image_entity) in enumerate(events.items()):
        moment = now_utc()
        async with session_scope(session_factory) as session:
            await DeliveryRepository(session).enqueue(
                source="home_assistant",
                delivery_key=delivery_key(
                    source="home_assistant",
                    event_type="camera.motion",
                    source_entity_id=image_entity,
                    occurred_at=moment,
                ),
                correlation_id=f"{camera_key}-context-test",
                raw_body={
                    "event_type": "camera.motion",
                    "camera": camera_key,
                    "entity_id": image_entity,
                    "timestamp": moment.isoformat(),
                    "metadata": {},
                },
            )
        worker = IngestWorker(
            session_factory=session_factory,
            settings=settings,
            cameras=cameras_config,
            # A distinct image per camera, so neither is dropped as a duplicate.
            ha_client=FakeHomeAssistant(image=_png(4, 3, tag=f"ctx-{index}".encode())),
            vision=SpyProvider(),
        )
        assert await worker.drain_once() is True

    assert set(seen) == {"gecko", "front_door"}

    gecko_prompt = build_system_prompt(seen["gecko"].context)
    assert GECKO_MARKER in gecko_prompt

    assert seen["front_door"].context.vision_context is None
    assert build_system_prompt(seen["front_door"].context) == BASE_VISION_PROMPT


# --------------------------------------------------------------------------- #
# The same guarantees through the Ollama provider
# --------------------------------------------------------------------------- #


async def test_ollama_sends_each_camera_its_own_context_and_nothing_else() -> None:
    """One provider instance, two cameras in turn: no context carries over."""
    import json

    import httpx

    from hermes_home.vision.ollama import OBSERVATION_SCHEMA, OllamaVisionProvider
    from hermes_home.vision.prompts import OLLAMA_JSON_GUIDANCE

    cameras = load_cameras_config(FIXTURE_CONFIG).cameras
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "message": {"role": "assistant", "content": '{"scene_summary": "a frame"}'},
                "done": True,
                "done_reason": "stop",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaVisionProvider(base_url="http://ollama.test:11434", model="m", client=client)

    order = ["gecko", "front_door", "gecko"]
    for key in order:
        result = await provider.analyze(
            VisionRequest(image=TINY_IMAGE, context=_context(key, cameras[key]))
        )
        assert result.ok, key

    for key, body in zip(order, bodies, strict=True):
        system, user = body["messages"]
        context = _context(key, cameras[key])
        assert system["content"] == build_system_prompt(context), key
        assert GECKO_MARKER not in user["content"], key
        assert user["content"].endswith(OLLAMA_JSON_GUIDANCE), key
        if key == "gecko":
            assert GECKO_MARKER in system["content"]
            assert system["content"].startswith(BASE_VISION_PROMPT)
        else:
            assert system["content"] == BASE_VISION_PROMPT
        # The schema is the same whatever the camera's context says.
        assert body["format"] == OBSERVATION_SCHEMA
        assert body["format"]["properties"] == SceneObservation.model_json_schema()["properties"]

    await client.aclose()
