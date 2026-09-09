"""The webhook: authentication, validation, and durable-then-202 behavior."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from hermes_home.api.app import create_app
from hermes_home.api.deps import AppState
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import create_verification_engine, session_scope
from hermes_home.storage.models import Event, EventDelivery
from hermes_home.vision.mock import MockVisionProvider

HEADER = "X-Hermes-Webhook-Secret"
PATH = "/api/v1/events/home-assistant"


@pytest.fixture
async def client(session_factory, settings, home_config, cameras_config, fake_ha):
    """The real app, with the worker left stopped so tests drive it explicitly."""
    app = create_app(settings)
    state = AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        engine=create_verification_engine(settings.database_url),
        verify_engine=create_verification_engine(settings.database_url),
        session_factory=session_factory,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
        worker=None,
    )
    app.state.app_state = state
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        http.hermes_state = state
        yield http


@pytest.fixture
def app_state(client):
    """The live AppState behind `client`, for tests that break it on purpose."""
    return client.hermes_state


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
    assert set(body["cameras"]) == {
        "backyard",
        "cottage",
        "front_door",
        "garage_left",
        "garage_right",
        "left_walkway",
        "right_walkway",
    }
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


# --------------------------------------------------------------------------- #
# Durability: a 202 is a promise, and it is verified before it is made
# --------------------------------------------------------------------------- #


async def test_202_means_a_fresh_connection_can_see_the_row(
    client, session_factory, settings
) -> None:
    """The response is only as good as what an uninvolved reader can see.

    A row is always visible to the connection that wrote it, so the writer's
    own success proves nothing. This opens a brand-new connection -- separate
    file handle, separate view of the WAL index -- and requires the row to be
    there.
    """
    from sqlalchemy import text

    from hermes_home.storage.engine import create_verification_engine

    response = await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})
    assert response.status_code == 202
    uid = response.json()["delivery_uid"]

    engine = create_verification_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT 1 FROM event_deliveries WHERE uid = :uid"), {"uid": uid}
            )
    finally:
        await engine.dispose()
    assert found is not None, "202 was returned for a row no other connection can see"


async def test_a_commit_that_does_not_persist_is_refused_not_accepted(client, app_state) -> None:
    """The 2026-09-09 failure, reproduced.

    Commits reported success while rows never became visible. Home Assistant
    was told 202, so it logged nothing and retried nothing, and four events
    vanished silently. Here the write is made to disappear the same way; the
    webhook must refuse rather than promise.
    """
    from hermes_home.storage.engine import create_verification_engine

    class BlindEngine:
        """A database in which nothing committed can ever be found again."""

        def connect(self):
            raise RuntimeError("database disk image is malformed")

    app_state.verify_engine = BlindEngine()
    try:
        response = await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})
    finally:
        app_state.verify_engine = create_verification_engine(app_state.settings.database_url)

    assert response.status_code == 503
    # Home Assistant's rest_command logs a warning on any non-2xx, which is
    # precisely the signal that was missing when this really happened.
    assert "durable" in response.json()["detail"]


async def test_a_missing_row_is_refused_even_without_an_error(client, app_state) -> None:
    """The subtler shape: no exception, the row simply is not there."""
    from hermes_home.storage.engine import create_verification_engine

    class EmptyResult:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def scalar(self, *a, **k):
            return None

    class AmnesiacEngine:
        def connect(self):
            return EmptyResult()

    app_state.verify_engine = AmnesiacEngine()
    try:
        response = await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})
    finally:
        app_state.verify_engine = create_verification_engine(app_state.settings.database_url)

    assert response.status_code == 503


async def test_a_refused_delivery_is_never_logged_as_accepted(client, app_state, capsys) -> None:
    """`webhook.accepted` is a durability claim and must not appear otherwise.

    Anyone reading the log for that line is entitled to believe the delivery
    exists.
    """
    from hermes_home.storage.engine import create_verification_engine

    class BlindEngine:
        def connect(self):
            raise RuntimeError("no")

    app_state.verify_engine = BlindEngine()
    try:
        await client.post(PATH, json=_payload(), headers={HEADER: "test-secret"})
    finally:
        app_state.verify_engine = create_verification_engine(app_state.settings.database_url)

    logged = capsys.readouterr().out
    assert "webhook.accepted" not in logged
    assert "webhook.durability_unconfirmed" in logged
