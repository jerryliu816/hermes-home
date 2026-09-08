"""Restart and recovery.

This service runs continuously in a home, so the question that matters is not
"does it work" but "what happens when it is killed mid-job". The answer has to be
that the delivery is picked up again and the event is created exactly once.

Exactly-once does not come from the queue -- it comes from the UNIQUE constraint
on ``events.delivery_key``. The queue only guarantees the work is not forgotten.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from hermes_home.core.ids import delivery_key
from hermes_home.core.time import now_utc
from hermes_home.ingest.worker import IngestWorker
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import DeliveryStatus, Disposition, Event
from hermes_home.storage.repositories import DeliveryRepository
from hermes_home.vision.mock import MockVisionProvider


def _worker(session_factory, settings, cameras_config, fake_ha, vision=None) -> IngestWorker:
    return IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=vision or MockVisionProvider(),
    )


async def _enqueue(session_factory, occurred_at=None) -> str:
    moment = occurred_at or now_utc()
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=delivery_key(
                source="home_assistant",
                event_type="camera.person_detected",
                source_entity_id="image.front_door_event_image",
                occurred_at=moment,
            ),
            correlation_id="recovery-test",
            raw_body={
                "event_type": "camera.person_detected",
                "camera": "front_door",
                "entity_id": "image.front_door_event_image",
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
        return delivery.uid


async def test_delivery_is_durable_before_any_processing(session_factory) -> None:
    """The 202 is only honest if the row is already committed."""
    uid = await _enqueue(session_factory)

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery is not None
        assert delivery.status == DeliveryStatus.PENDING
        assert delivery.raw_body is not None


async def test_crashed_worker_lease_expires_and_work_is_recovered(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """Simulate a hard kill mid-job, then restart: the event still gets created."""
    uid = await _enqueue(session_factory)

    # Claim it, then vanish without finishing -- exactly what SIGKILL leaves.
    async with session_scope(session_factory) as session:
        claimed = await DeliveryRepository(session).claim_next(lease_seconds=120)
        assert claimed is not None
        assert claimed.status == DeliveryStatus.PROCESSING

    # A fresh worker must not steal a job whose lease is still valid.
    assert await _worker(session_factory, settings, cameras_config, fake_ha).drain_once() is False

    # Wind the lease into the past, as real elapsed time would.
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        delivery.lease_expires_at = now_utc() - timedelta(seconds=1)

    # Restarted worker reclaims and completes it.
    assert await _worker(session_factory, settings, cameras_config, fake_ha).drain_once() is True

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery.status == DeliveryStatus.COMPLETED
        assert delivery.disposition == Disposition.ACCEPTED
        assert delivery.attempts == 2, "the crashed attempt is counted, not hidden"
        assert await session.scalar(select(func.count()).select_from(Event)) == 1


async def test_recovery_creates_the_event_exactly_once(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """A delivery replayed after a crash must not produce a second event.

    Asserted through the UNIQUE constraint's effect, not by counting attempts:
    the second pass is deliberately allowed to run the whole pipeline again.
    """
    uid = await _enqueue(session_factory)
    await _worker(session_factory, settings, cameras_config, fake_ha).drain_once()

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        original_event_id = delivery.event_id
        # Force a full replay of an already-completed delivery.
        delivery.status = DeliveryStatus.PENDING
        delivery.next_attempt_at = now_utc()
        delivery.disposition = None

    fake_ha.image_state_ts = now_utc() + timedelta(seconds=60)
    assert await _worker(session_factory, settings, cameras_config, fake_ha).drain_once() is True

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery.disposition == Disposition.DUPLICATE_DELIVERY
        assert delivery.event_id == original_event_id


async def test_two_workers_cannot_claim_the_same_delivery(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """Claiming is a single atomic UPDATE, so concurrency needs no extra lock."""
    await _enqueue(session_factory)

    async with session_scope(session_factory) as first_session:
        claimed = await DeliveryRepository(first_session).claim_next(lease_seconds=120)
        assert claimed is not None

    async with session_scope(session_factory) as second_session:
        assert await DeliveryRepository(second_session).claim_next(lease_seconds=120) is None


async def test_transient_failure_is_retried_with_backoff(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """Home Assistant being briefly unreachable must not lose the delivery."""
    from hermes_home.core.errors import HomeAssistantError

    uid = await _enqueue(session_factory)
    fake_ha.raise_on_state = HomeAssistantError("ha_timeout", "HA unreachable", retryable=True)

    assert await _worker(session_factory, settings, cameras_config, fake_ha).drain_once() is True

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery.status == DeliveryStatus.PENDING, "queued for another attempt"
        assert delivery.last_error_code == "ha_timeout"
        assert delivery.last_error_message
        assert delivery.next_attempt_at > now_utc(), "backoff must be applied"

    # Once HA recovers, the delivery completes without any manual intervention.
    fake_ha.raise_on_state = None
    async with session_scope(session_factory) as session:
        (await DeliveryRepository(session).get_by_uid(uid)).next_attempt_at = now_utc()

    assert await _worker(session_factory, settings, cameras_config, fake_ha).drain_once() is True
    async with session_scope(session_factory) as session:
        assert (
            await DeliveryRepository(session).get_by_uid(uid)
        ).status == DeliveryStatus.COMPLETED


async def test_retries_are_bounded_and_end_in_failed(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """No infinite retry loop: a persistently broken delivery must come to rest."""
    from hermes_home.core.errors import HomeAssistantError

    settings.ingest_max_attempts = 3
    uid = await _enqueue(session_factory)
    fake_ha.raise_on_state = HomeAssistantError("ha_timeout", "HA down", retryable=True)

    worker = _worker(session_factory, settings, cameras_config, fake_ha)
    for _ in range(settings.ingest_max_attempts):
        async with session_scope(session_factory) as session:
            delivery = await DeliveryRepository(session).get_by_uid(uid)
            if delivery.status == DeliveryStatus.PENDING:
                delivery.next_attempt_at = now_utc()
        await worker.drain_once()

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.attempts == settings.ingest_max_attempts
        assert delivery.last_error_code == "ha_timeout"

    # And a failed delivery is not silently re-run.
    assert await worker.drain_once() is False


async def test_raw_body_retention_keeps_metadata(session_factory, settings) -> None:
    """Pruning removes the payload and nothing else."""
    from hermes_home.ingest.retention import prune_raw_bodies

    uid = await _enqueue(session_factory)
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        delivery.received_at = now_utc() - timedelta(days=30)

    assert await prune_raw_bodies(session_factory, retention_days=14) == 1

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery.raw_body is None, "the payload goes"
        assert delivery.received_at is not None, "the metadata stays"
        assert delivery.delivery_key
        assert delivery.correlation_id
        assert delivery.status


async def test_recent_raw_bodies_are_kept(session_factory) -> None:
    from hermes_home.ingest.retention import prune_raw_bodies

    uid = await _enqueue(session_factory)
    assert await prune_raw_bodies(session_factory, retention_days=14) == 0

    async with session_scope(session_factory) as session:
        assert (await DeliveryRepository(session).get_by_uid(uid)).raw_body is not None
