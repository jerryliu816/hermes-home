"""The Ollama provider through the real worker and pipeline, on a disposable DB.

What is defended: provenance and usage land in ``event_analyses`` as the
provider reported them, the pipeline's single bounded retry behaves the same as
for every other provider, a failed analysis never loses the physical event, and
nothing is ever sent anywhere but the configured Ollama host -- there is no
fallback to a cloud provider or the mock.
"""

from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import func, select

from hermes_home.core.ids import delivery_key
from hermes_home.core.time import now_utc
from hermes_home.ingest.worker import IngestWorker
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Event, EventTag
from hermes_home.storage.repositories import DeliveryRepository, EventRepository
from hermes_home.vision.ollama import OllamaVisionProvider
from hermes_home.vision.prompts import OLLAMA_PROMPT_VERSION

OLLAMA_HOST = "ollama.test"
MODEL = "gemma4:31b"
TRIGGER_ENTITY = "image.front_door_event_image"

OBSERVATION = {
    "scene_summary": "A person carrying a box at the door.",
    "person_count": 1,
    "vehicle_count": None,
    "package_present": True,
    "tags": ["person_present", "package_present"],
}


def _ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": MODEL,
            "message": {"role": "assistant", "content": json.dumps(OBSERVATION)},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 900,
            "eval_count": 120,
        },
    )


class ScriptedOllama:
    """Replays responses in order and refuses any host but the Ollama one."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == OLLAMA_HOST, f"request left for {request.url.host}"
        self.paths.append(request.url.path)
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch) -> None:
    import hermes_home.ingest.pipeline as pipeline

    monkeypatch.setattr(pipeline, "_VISION_RETRY_DELAY_SECONDS", 0.0)


async def _process(session_factory, settings, cameras_config, fake_ha, script: ScriptedOllama):
    moment = now_utc()
    async with session_scope(session_factory) as session:
        await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=delivery_key(
                source="home_assistant",
                event_type="camera.person_detected",
                source_entity_id=TRIGGER_ENTITY,
                occurred_at=moment,
            ),
            correlation_id="ollama-pipeline-test",
            raw_body={
                "event_type": "camera.person_detected",
                "camera": "front_door",
                "entity_id": TRIGGER_ENTITY,
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
    client = httpx.AsyncClient(transport=httpx.MockTransport(script))
    provider = OllamaVisionProvider(
        base_url=f"http://{OLLAMA_HOST}:11434", model=MODEL, client=client
    )
    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=provider,
    )
    assert await worker.drain_once() is True
    await client.aclose()

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        assert event is not None, "the physical event is always kept"
        analyses = await EventRepository(session).analyses_for(event.id)
        tags = await session.scalar(
            select(func.count()).select_from(EventTag).where(EventTag.event_id == event.id)
        )
        return event, analyses, tags


async def test_success_stores_ollama_provenance_and_usage(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    script = ScriptedOllama(_ok())
    _event, analyses, tags = await _process(
        session_factory, settings, cameras_config, fake_ha, script
    )

    assert len(analyses) == 1
    row = analyses[0]
    assert row.status == "ok"
    assert row.provider == "ollama"
    assert row.model == MODEL
    assert row.prompt_version == OLLAMA_PROMPT_VERSION
    assert row.input_tokens == 900
    assert row.output_tokens == 120
    assert row.cost_usd == 0.0
    assert row.observation["person_count"] == 1
    # Omitted by the model, so unknown -- stored as an explicit null, not 0.
    assert "animal_count" in row.observation
    assert row.observation["animal_count"] is None
    assert tags == 2
    assert script.paths == ["/api/chat"]


async def test_transient_failure_then_success_keeps_both_attempts(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    script = ScriptedOllama(httpx.Response(503), _ok())
    _event, analyses, tags = await _process(
        session_factory, settings, cameras_config, fake_ha, script
    )

    assert [a.status for a in analyses] == ["failed", "ok"]
    assert analyses[0].error_code == "server_error"
    assert analyses[0].retryable is True
    assert analyses[0].provider == analyses[1].provider == "ollama"
    assert tags == 2


async def test_two_transient_failures_keep_the_event_without_observation(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    script = ScriptedOllama(httpx.Response(503), httpx.Response(503))
    _event, analyses, tags = await _process(
        session_factory, settings, cameras_config, fake_ha, script
    )

    assert [a.status for a in analyses] == ["failed", "failed"]
    assert all(a.observation is None for a in analyses)
    assert all(a.provider == "ollama" for a in analyses), "no fallback provider ran"
    assert tags == 0
    assert len(script.paths) == 2, "exactly the pipeline's bounded retry, no hidden loop"


async def test_connection_refused_is_retried_once_then_recorded(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    calls = 0

    def refuse(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.host == OLLAMA_HOST
        raise httpx.ConnectError("refused", request=request)

    class Refusing(ScriptedOllama):
        def __call__(self, request):  # type: ignore[override]
            return refuse(request)

    _event, analyses, _tags = await _process(
        session_factory, settings, cameras_config, fake_ha, Refusing()
    )
    assert calls == 2
    assert [a.error_code for a in analyses] == ["server_error", "server_error"]


async def test_missing_model_is_terminal_and_not_retried(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    script = ScriptedOllama(httpx.Response(404, json={"error": "model not found"}))
    _event, analyses, _tags = await _process(
        session_factory, settings, cameras_config, fake_ha, script
    )

    assert len(analyses) == 1
    assert analyses[0].status == "failed"
    assert analyses[0].retryable is False
    assert "model not found" not in (analyses[0].error_message or ""), (
        "the response body is not copied into the row"
    )


async def test_invalid_observation_is_terminal_and_stores_no_observation(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    bad = httpx.Response(
        200,
        json={
            "model": MODEL,
            "message": {"role": "assistant", "content": "Looks like a delivery."},
            "done": True,
            "done_reason": "stop",
        },
    )
    _event, analyses, tags = await _process(
        session_factory, settings, cameras_config, fake_ha, ScriptedOllama(bad)
    )
    assert len(analyses) == 1
    assert analyses[0].error_code == "bad_response"
    assert analyses[0].observation is None
    assert tags == 0
