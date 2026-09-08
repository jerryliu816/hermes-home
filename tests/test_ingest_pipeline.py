"""The ingest pipeline: the acceptance gate for Milestone 4.

Covers the full path with a fake Home Assistant and the mock vision provider,
plus the two dedupe stages, the freshness gate, vision retry, and correlation.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from hermes_home.core.ids import delivery_key
from hermes_home.core.time import now_utc
from hermes_home.ingest.worker import IngestWorker
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import (
    Disposition,
    Event,
    EventAnalysis,
    EventDelivery,
    EventTag,
    Incident,
)
from hermes_home.storage.repositories import DeliveryRepository, EventRepository
from hermes_home.vision.mock import MockVisionProvider
from tests.conftest import OTHER_IMAGE, FakeHomeAssistant

TRIGGER_ENTITY = "image.front_door_event_image"


async def _enqueue(session_factory, *, occurred_at=None, event_type="camera.person_detected"):
    moment = occurred_at or now_utc()
    key = delivery_key(
        source="home_assistant",
        event_type=event_type,
        source_entity_id=TRIGGER_ENTITY,
        occurred_at=moment,
    )
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=key,
            correlation_id="test-correlation",
            raw_body={
                "event_type": event_type,
                "camera": "front_door",
                "entity_id": TRIGGER_ENTITY,
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
        return delivery.uid


async def _run_worker_once(session_factory, settings, cameras_config, fake_ha, vision=None):
    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=vision or MockVisionProvider(),
    )
    return await worker.drain_once()


# --------------------------------------------------------------------------- #
# The headline test
# --------------------------------------------------------------------------- #


async def test_end_to_end_delivery_becomes_stored_event(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """webhook delivery -> fake HA image -> mock vision -> real SQLite rows."""
    delivery_uid = await _enqueue(session_factory)

    assert await _run_worker_once(session_factory, settings, cameras_config, fake_ha) is True

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(delivery_uid)
        assert delivery is not None
        assert delivery.status == "completed"
        assert delivery.disposition == Disposition.ACCEPTED
        assert delivery.event_id is not None

        event = await session.get(Event, delivery.event_id)
        assert event is not None
        assert event.event_type == "camera.person_detected"
        assert event.source_entity_id == TRIGGER_ENTITY
        assert event.zone_id is not None, "the event must resolve to a zone"
        assert event.content_hash is not None
        assert event.occurred_at.tzinfo is not None, "timestamps must stay tz-aware"
        assert event.payload["camera"] == "front_door"

        analyses = await EventRepository(session).analyses_for(event.id)
        assert len(analyses) == 1
        assert analyses[0].status == "ok"
        assert analyses[0].provider == "mock"
        assert analyses[0].prompt_version
        assert analyses[0].observation is not None
        assert analyses[0].observation["scene_summary"]
        # Image metadata survives even though the image itself does not.
        assert analyses[0].artifact_bytes is not None
        assert analyses[0].artifact_width == 4
        assert analyses[0].artifact_height == 3

        # An unknown must remain null, never coerced to zero.
        assert analyses[0].observation["vehicle_count"] is None

    assert fake_ha.image_calls == 1


async def test_image_is_never_persisted(session_factory, settings, cameras_config, fake_ha) -> None:
    """Retrieve, analyze, discard: no table should contain image bytes."""
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    async with session_scope(session_factory) as session:
        analysis = await session.scalar(select(EventAnalysis))
        assert analysis is not None
        serialized = str(analysis.observation)
        assert "\\xff\\xd8" not in serialized
        assert len(serialized) < 4000


# --------------------------------------------------------------------------- #
# Deduplication
# --------------------------------------------------------------------------- #


async def test_duplicate_delivery_creates_no_second_event(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """The same delivery twice: one event, a duplicate counter, an audit row."""
    moment = now_utc()
    await _enqueue(session_factory, occurred_at=moment)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    second_uid = await _enqueue(session_factory, occurred_at=moment)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1

        event = await session.scalar(select(Event))
        assert event.duplicate_count == 1

        # The suppressed delivery is auditable and points at the canonical event.
        duplicate = await DeliveryRepository(session).get_by_uid(second_uid)
        assert duplicate.disposition == Disposition.DUPLICATE_DELIVERY
        assert duplicate.event_id == event.id


async def test_same_image_within_window_is_content_duplicate(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """Different delivery keys, identical bytes moments apart: one occurrence."""
    first = now_utc()
    await _enqueue(session_factory, occurred_at=first)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    # A second trigger 2s later, but the camera served the very same frame.
    await _enqueue(session_factory, occurred_at=first + timedelta(seconds=2))
    fake_ha.image_state_ts = now_utc() + timedelta(seconds=2)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        event = await session.scalar(select(Event))
        assert event.duplicate_count == 1


async def test_content_duplicate_does_not_call_vision(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """Hashing before analyzing is the whole point of stage B's position."""

    class CountingVision(MockVisionProvider):
        calls = 0

        async def analyze(self, request):  # type: ignore[override]
            CountingVision.calls += 1
            return await super().analyze(request)

        async def health_check(self) -> bool:
            return True

    vision = CountingVision()
    first = now_utc()
    await _enqueue(session_factory, occurred_at=first)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha, vision)
    assert CountingVision.calls == 1

    await _enqueue(session_factory, occurred_at=first + timedelta(seconds=1))
    fake_ha.image_state_ts = now_utc() + timedelta(seconds=5)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha, vision)

    assert CountingVision.calls == 1, "a duplicate frame must not be paid for twice"


async def test_different_image_outside_window_is_a_new_event(
    session_factory, settings, cameras_config
) -> None:
    ha = FakeHomeAssistant()
    first = now_utc() - timedelta(minutes=5)
    await _enqueue(session_factory, occurred_at=first)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    ha.image = OTHER_IMAGE
    ha.image_state_ts = now_utc()
    await _enqueue(session_factory, occurred_at=now_utc())
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 2


# --------------------------------------------------------------------------- #
# Freshness gate
# --------------------------------------------------------------------------- #


async def test_stale_event_image_is_rejected_not_misfiled(
    session_factory, settings, cameras_config
) -> None:
    """Refusing beats describing the previous event under the current timestamp."""
    ha = FakeHomeAssistant(image_state_ts=now_utc())

    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    # A new trigger, but the image entity's timestamp never advances.
    second_uid = await _enqueue(session_factory, occurred_at=now_utc() + timedelta(seconds=30))
    ha.image = OTHER_IMAGE  # bytes differ, so only the timestamp can catch this
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(second_uid)
        assert delivery.status == "rejected"
        assert delivery.disposition == Disposition.REJECTED_STALE_IMAGE
        assert await session.scalar(select(func.count()).select_from(Event)) == 1


async def test_advancing_image_timestamp_is_accepted(
    session_factory, settings, cameras_config
) -> None:
    ha = FakeHomeAssistant(image_state_ts=now_utc())
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    ha.image = OTHER_IMAGE
    ha.image_state_ts = now_utc() + timedelta(seconds=30)
    await _enqueue(session_factory, occurred_at=now_utc() + timedelta(seconds=30))
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 2


# --------------------------------------------------------------------------- #
# Vision failure
# --------------------------------------------------------------------------- #


async def test_vision_failure_keeps_the_event_and_records_both_attempts(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """A vision outage must not lose the physical observation."""
    vision = MockVisionProvider(script=["failed_retryable", "ok"])
    settings.vision_timeout_seconds = 1.0

    import hermes_home.ingest.pipeline as pipeline

    pipeline._VISION_RETRY_DELAY_SECONDS = 0.0  # keep the test fast

    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha, vision)

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        assert event is not None, "the event survives a failed analysis"

        analyses = await EventRepository(session).analyses_for(event.id)
        assert [a.status for a in analyses] == ["failed", "ok"]
        assert analyses[0].error_code == "timeout"
        assert analyses[0].observation is None
        assert analyses[1].observation is not None


async def test_terminal_vision_failure_is_not_retried(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    vision = MockVisionProvider(script=["failed_terminal", "ok"])
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha, vision)

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        analyses = await EventRepository(session).analyses_for(event.id)
        assert len(analyses) == 1, "a non-retryable failure must not be retried"
        assert analyses[0].status == "failed"


async def test_provider_that_raises_does_not_lose_the_event(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    class ExplodingVision(MockVisionProvider):
        async def analyze(self, request):  # type: ignore[override]
            raise RuntimeError("provider bug")

    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha, ExplodingVision())

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        assert event is not None
        analyses = await EventRepository(session).analyses_for(event.id)
        assert analyses[0].status == "failed"
        assert analyses[0].error_code == "unexpected"


# --------------------------------------------------------------------------- #
# Tags and correlation
# --------------------------------------------------------------------------- #


async def test_vision_tags_are_recorded(session_factory, settings, cameras_config, fake_ha) -> None:
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        tags = await EventRepository(session).tags_for(event.id)
        analysis = await session.scalar(select(EventAnalysis))
        assert sorted(analysis.observation["tags"]) == tags
        for tag in tags:
            row = await session.scalar(select(EventTag).where(EventTag.tag == tag))
            assert row.source == "vision"


async def test_nearby_events_share_an_incident(session_factory, settings, cameras_config) -> None:
    ha = FakeHomeAssistant(image_state_ts=now_utc())
    base = now_utc()

    await _enqueue(session_factory, occurred_at=base)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    ha.image = OTHER_IMAGE
    ha.image_state_ts = base + timedelta(seconds=30)
    await _enqueue(session_factory, occurred_at=base + timedelta(seconds=30))
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Incident)) == 1
        events = list((await session.scalars(select(Event))).all())
        assert len({e.incident_id for e in events}) == 1
        assert all(e.incident_id is not None for e in events)


async def test_distant_events_are_separate_incidents(
    session_factory, settings, cameras_config
) -> None:
    ha = FakeHomeAssistant(image_state_ts=now_utc())
    base = now_utc() - timedelta(hours=2)

    await _enqueue(session_factory, occurred_at=base)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    ha.image = OTHER_IMAGE
    ha.image_state_ts = now_utc()
    await _enqueue(session_factory, occurred_at=now_utc())
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Incident)) == 2


async def test_unknown_camera_is_rejected(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key="unknown-camera-key",
            correlation_id="c",
            raw_body={
                "event_type": "camera.person_detected",
                "camera": "not_a_camera",
                "entity_id": "image.nope",
                "timestamp": now_utc().isoformat(),
            },
        )
        uid = delivery.uid

    await _run_worker_once(session_factory, settings, cameras_config, fake_ha)

    async with session_scope(session_factory) as session:
        stored = await DeliveryRepository(session).get_by_uid(uid)
        assert stored.status == "rejected"
        assert stored.disposition == Disposition.REJECTED_INVALID


# --------------------------------------------------------------------------- #
# Stale frames when the entity publishes no timestamp
#
# Found on real hardware: after a Home Assistant restart or integration reload,
# an image.* entity resets to "unknown" while image_proxy keeps serving the
# PREVIOUS event's frame. The timestamp gate cannot see it, so content is the
# only remaining evidence.
# --------------------------------------------------------------------------- #


async def test_unknown_state_with_unchanged_bytes_is_rejected(
    session_factory, settings, cameras_config
) -> None:
    ha = FakeHomeAssistant(image_state_ts=now_utc())

    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    # Simulate the reload: no usable state, same image still served.
    ha.state_value = "unknown"
    await _enqueue(session_factory, occurred_at=now_utc() + timedelta(minutes=10))
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1, (
            "a stale frame must not become a second event"
        )
        delivery = await session.scalar(
            select(EventDelivery).order_by(EventDelivery.id.desc()).limit(1)
        )
        assert delivery.disposition == Disposition.REJECTED_STALE_IMAGE


async def test_unknown_state_with_new_bytes_is_accepted(
    session_factory, settings, cameras_config
) -> None:
    """A genuinely new frame must still get through when the timestamp is absent.

    Rejecting on 'unknown' alone would drop real events; the content comparison
    is what makes the fallback precise rather than merely cautious.
    """
    ha = FakeHomeAssistant(image_state_ts=now_utc())

    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    ha.state_value = "unknown"
    ha.image = OTHER_IMAGE
    await _enqueue(session_factory, occurred_at=now_utc() + timedelta(minutes=10))
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 2


async def test_first_ever_event_is_accepted_without_a_timestamp(
    session_factory, settings, cameras_config
) -> None:
    """With no prior event there is nothing to compare against; accept it."""
    ha = FakeHomeAssistant(image_state_ts=now_utc())
    ha.state_value = "unknown"

    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        assert event is not None
        assert event.source_state_ts is None, "no timestamp was available to record"


async def test_waits_for_an_event_still_that_arrives_late(
    session_factory, settings, cameras_config
) -> None:
    """The real failure mode: the trigger beats the image.

    Measured on Eufy hardware, the event still lands ~3.7s after the detection
    fires, while the entity reports "unknown" in the meantime. Giving up early
    rejects a perfectly good event, so the gate must keep waiting.
    """
    ha = FakeHomeAssistant(image_state_ts=now_utc())
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    # Next trigger: entity is "unknown" and serving the old frame, until poll 4.
    ha.state_value = "unknown"
    ha.publish_on_call = 4
    ha.pending_image = OTHER_IMAGE
    ha.pending_state_ts = now_utc() + timedelta(seconds=30)

    await _enqueue(session_factory, occurred_at=now_utc() + timedelta(seconds=30))
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 2, (
            "a late-arriving event still must be waited for, not rejected"
        )
        newest = await session.scalar(select(Event).order_by(Event.id.desc()).limit(1))
        assert newest.source_state_ts is not None, "the advanced timestamp is recorded"
    assert ha.state_calls >= 4, "the gate must actually have polled while waiting"


async def test_gives_up_after_the_configured_budget(
    session_factory, settings, cameras_config
) -> None:
    """Waiting is bounded: an image that never arrives must not hang the worker."""
    settings.freshness_poll_attempts = 3
    settings.freshness_poll_interval_seconds = 0.001

    ha = FakeHomeAssistant(image_state_ts=now_utc())
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    # Timestamp never advances and the frame never changes.
    await _enqueue(session_factory, occurred_at=now_utc() + timedelta(seconds=30))
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        delivery = await session.scalar(
            select(EventDelivery).order_by(EventDelivery.id.desc()).limit(1)
        )
        assert delivery.disposition == Disposition.REJECTED_STALE_IMAGE


async def test_stale_frame_rejected_even_when_history_has_no_timestamp(
    session_factory, settings, cameras_config
) -> None:
    """Regression: observed on real hardware attaching a 255s-old frame.

    The first event recorded no ``source_state_ts`` (its entity was reporting
    "unknown"), so a history-based check had nothing to compare against and
    accepted whatever the entity happened to be serving. The freshness test must
    be anchored to the trigger time instead, which does not depend on history.
    """
    settings.freshness_poll_attempts = 2
    settings.freshness_poll_interval_seconds = 0.001

    ha = FakeHomeAssistant(image_state_ts=now_utc())
    ha.state_value = "unknown"  # forces the no-timestamp path for event 1
    await _enqueue(session_factory)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        first = await session.scalar(select(Event))
        assert first is not None
        assert first.source_state_ts is None, "precondition: no timestamp in history"

    # A trigger four minutes later, while the entity still serves the old frame
    # under its old timestamp.
    ha.state_value = None
    trigger = now_utc() + timedelta(minutes=4)
    await _enqueue(session_factory, occurred_at=trigger)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1, (
            "a frame older than the trigger must not become an event"
        )
        delivery = await session.scalar(
            select(EventDelivery).order_by(EventDelivery.id.desc()).limit(1)
        )
        assert delivery.disposition == Disposition.REJECTED_STALE_IMAGE


async def test_frame_newer_than_the_trigger_is_accepted(
    session_factory, settings, cameras_config
) -> None:
    """The normal case: the still lands shortly after the trigger."""
    trigger = now_utc()
    ha = FakeHomeAssistant(image_state_ts=trigger + timedelta(seconds=4))

    await _enqueue(session_factory, occurred_at=trigger)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        assert event is not None
        assert event.source_state_ts is not None
        assert event.source_state_ts > event.occurred_at


async def test_frame_slightly_predating_the_trigger_is_tolerated(
    session_factory, settings, cameras_config
) -> None:
    """An integration that stamps at capture rather than receipt still works."""
    trigger = now_utc()
    ha = FakeHomeAssistant(image_state_ts=trigger - timedelta(seconds=2))
    settings.freshness_tolerance_seconds = 5.0

    await _enqueue(session_factory, occurred_at=trigger)
    await _run_worker_once(session_factory, settings, cameras_config, ha)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
