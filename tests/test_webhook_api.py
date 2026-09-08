"""The webhook: authentication, validation, and durable-then-202 behavior."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from hermes_home.api.app import create_app
from hermes_home.api.deps import AppState
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Event, EventDelivery
from hermes_home.vision.mock import MockVisionProvider

HEADER = "X-Hermes-Webhook-Secret"
PATH = "/api/v1/events/home-assistant"


@pytest.fixture
async def client(session_factory, settings, home_config, cameras_config, fake_ha):
    """The real app, with the worker left stopped so tests drive it explicitly."""
    app = create_app(settings)
    app.state.app_state = AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        engine=None,
        session_factory=session_factory,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
        worker=None,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _payload(**overrides) -> dict:
    body = {
        "event_type": "camera.person_detected",
        "camera": "front_door",
        "entity_id": "image.front_door_event_image",
        "timestamp": now_utc().isoformat(),
        "metadata": {"source": "eufy"},
    }
    body.update(overrides)
    return body


async def test_valid_request_is_accepted_with_202(client, session_factory) -> None:
    response = await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["delivery_uid"]
    assert response.headers["X-Correlation-Id"]


async def test_delivery_is_committed_before_the_response(client, session_factory) -> None:
    """Durability before acknowledgement: HA is told 'accepted' only once it is."""
    response = await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})
    uid = response.json()["delivery_uid"]

    async with session_scope(session_factory) as session:
        stored = await session.scalar(select(EventDelivery).where(EventDelivery.uid == uid))
        assert stored is not None
        assert stored.status == "pending"
        assert stored.raw_body is not None


async def test_webhook_does_not_wait_for_analysis(client, session_factory) -> None:
    """No event exists yet -- that is the entire point of the async design."""
    await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_wrong_secret_is_rejected(client, session_factory) -> None:
    response = await client.post(PATH, json=_payload(), headers={HEADER: "wrong-secret"})
    assert response.status_code == 401

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(EventDelivery)) == 0


async def test_missing_secret_is_rejected(client) -> None:
    assert (await client.post(PATH, json=_payload())).status_code == 401


async def test_malformed_payload_is_rejected(client) -> None:
    response = await client.post(
        PATH, json={"camera": "front_door"}, headers={HEADER: "test-secret"}
    )
    assert response.status_code == 422


async def test_unknown_event_type_is_rejected(client) -> None:
    response = await client.post(
        PATH, json=_payload(event_type="camera.teleportation"), headers={HEADER: "test-secret"}
    )
    assert response.status_code == 422
    assert "known types" in response.json()["detail"]


async def test_offsetless_timestamp_is_rejected(client) -> None:
    """Guessing a timezone here would poison every later temporal query."""
    response = await client.post(
        PATH, json=_payload(timestamp="2026-09-08T12:00:00"), headers={HEADER: "test-secret"}
    )
    assert response.status_code == 422


async def test_oversized_body_is_rejected(client, settings) -> None:
    payload = _payload(metadata={"junk": "x" * (settings.webhook_max_body_bytes + 1000)})
    response = await client.post(PATH, json=payload, headers={HEADER: "test-secret"})
    assert response.status_code == 413


async def test_secret_is_not_echoed_in_any_response(client) -> None:
    response = await client.post(PATH, json=_payload(), headers={HEADER: "wrong"})
    assert "test-secret" not in response.text


async def test_health_reports_queue_depth(client) -> None:
    await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})

    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["queue"]["pending"] == 1
    assert body["cameras"] == ["front_door"]
    # The mock provider means no model is in play, and no key is ever exposed.
    assert body["vision_model"] is None
    assert "token" not in response.text.lower()


async def test_full_path_webhook_to_stored_event(
    client, session_factory, settings, cameras_config, fake_ha
) -> None:
    """The acceptance gate, through the real HTTP surface:
    signed webhook -> 202 -> worker -> fake HA -> mock vision -> SQLite."""
    from hermes_home.ingest.worker import IngestWorker

    response = await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})
    assert response.status_code == 202
    delivery_uid = response.json()["delivery_uid"]

    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
    )
    assert await worker.drain_once() is True

    async with session_scope(session_factory) as session:
        delivery = await session.scalar(
            select(EventDelivery).where(EventDelivery.uid == delivery_uid)
        )
        assert delivery.status == "completed"

        event = await session.get(Event, delivery.event_id)
        assert event is not None
        assert event.event_type == "camera.person_detected"
        assert event.zone_id is not None
        assert event.occurred_at.tzinfo is not None
