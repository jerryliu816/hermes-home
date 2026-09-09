"""Detecting Home Assistant events that never reached us.

This exists because of a real incident. On 2026-09-09 four camera triggers
fired between 07:30 and 07:36; every camera reported healthy, hermes-home was
polling Home Assistant successfully throughout, all four automations ran, and
all four deliveries vanished in transit. Nothing noticed. The stored history
simply had a hole in it shaped exactly like a quiet morning.

Camera health cannot see this failure, which is why it is a separate axis.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from hermes_home.config import CameraConfig, CamerasConfig
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.ids import delivery_key
from hermes_home.core.time import now_utc
from hermes_home.health.reconcile import DeliveryReconciler, rising_edges
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import PipelineStatus, VerificationMode
from hermes_home.storage.repositories import DeliveryGapRepository, DeliveryRepository

UTC = ZoneInfo("UTC")


class HistoryHA:
    """A Home Assistant whose recorder history can be scripted per entity."""

    def __init__(self) -> None:
        self.history: dict[str, list[tuple[datetime, str]]] = {}
        self.raise_for_all: Exception | None = None
        self.calls = 0

    async def get_state_history(self, entity_id, *, start, end=None):
        self.calls += 1
        if self.raise_for_all is not None:
            raise self.raise_for_all
        return [(t, s) for t, s in self.history.get(entity_id, []) if t >= start]

    async def aclose(self) -> None:
        return None


def camera(key: str = "test", trigger: str | None = "binary_sensor.test_motion_detected"):
    return CameraConfig(
        name=key.title(),
        camera_entity=f"camera.{key}",
        event_image_entity=f"image.{key}_event_image",
        event_image_strategy="image_entity_state",
        trigger_entity=trigger,
        location="front_entry",
    )


def cameras_of(**kw) -> CamerasConfig:
    return CamerasConfig(cameras=kw)


def reconciler(session_factory, settings, cameras, ha) -> DeliveryReconciler:
    return DeliveryReconciler(
        session_factory=session_factory, settings=settings, cameras=cameras, ha_client=ha
    )


def fired(ha: HistoryHA, entity: str, *times: datetime) -> None:
    """Script an on/off cycle around each detection, as real hardware does."""
    seq: list[tuple[datetime, str]] = []
    for t in times:
        seq.append((t, "on"))
        seq.append((t + timedelta(seconds=11), "off"))
    ha.history[entity] = sorted(seq)


async def deliver(session_factory, camera_key: str, entity: str, at: datetime) -> None:
    async with session_scope(session_factory) as session:
        await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=delivery_key(
                source="home_assistant",
                event_type="camera.motion",
                source_entity_id=entity,
                occurred_at=at,
            ),
            correlation_id=f"test-{at.timestamp()}",
            raw_body={
                "event_type": "camera.motion",
                "camera": camera_key,
                "entity_id": entity,
                "timestamp": at.isoformat(),
            },
            received_at=at,
        )


async def open_gaps(session_factory, camera_key: str | None = None) -> list:
    async with session_scope(session_factory) as session:
        return await DeliveryGapRepository(session).gaps_between(
            start=now_utc() - timedelta(days=2),
            end=now_utc() + timedelta(days=2),
            camera_key=camera_key,
        )


# --------------------------------------------------------------------------- #
# The trigger signal
# --------------------------------------------------------------------------- #


def test_only_rising_edges_count() -> None:
    """A detection holds the sensor on for seconds; the automation fires once.

    Counting every `on` sample would invent gaps for deliveries nobody promised.
    """
    base = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
    history = [
        (base, "on"),
        (base + timedelta(seconds=2), "on"),
        (base + timedelta(seconds=11), "off"),
        (base + timedelta(minutes=3), "on"),
        (base + timedelta(minutes=3, seconds=11), "off"),
    ]
    assert rising_edges(history) == [base, base + timedelta(minutes=3)]


def test_an_entity_that_never_fires_yields_no_edges() -> None:
    base = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
    assert rising_edges([(base, "off"), (base + timedelta(minutes=5), "off")]) == []


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


async def test_trigger_with_matching_delivery_is_no_gap(session_factory, settings) -> None:
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    fired(ha, "binary_sensor.test_motion_detected", when)
    await deliver(session_factory, "test", "image.test_event_image", when)

    outcome = (
        await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()
    )[0]

    assert outcome.triggers == 1
    assert outcome.matched == 1
    assert outcome.missing == 0
    assert await open_gaps(session_factory) == []


async def test_trigger_without_delivery_is_detected(session_factory, settings) -> None:
    """The 2026-09-09 case, reduced."""
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    fired(ha, "binary_sensor.test_motion_detected", when)

    outcome = (
        await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()
    )[0]

    assert outcome.missing == 1
    gaps = await open_gaps(session_factory)
    assert len(gaps) == 1
    assert gaps[0].camera_key == "test"
    assert gaps[0].reason == "webhook_not_received"
    assert gaps[0].trigger_entity == "binary_sensor.test_motion_detected"


async def test_several_missed_triggers_are_counted_separately(session_factory, settings) -> None:
    """Four lost deliveries must read as four, not as 'something went wrong'.

    Home Assistant's recorder keeps a timestamp per transition, which is the
    only reason this distinction is available at all.
    """
    ha = HistoryHA()
    base = now_utc() - timedelta(minutes=30)
    fired(
        ha,
        "binary_sensor.test_motion_detected",
        base,
        base + timedelta(minutes=3),
        base + timedelta(minutes=5),
        base + timedelta(minutes=8),
    )

    await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()

    gaps = await open_gaps(session_factory)
    assert len(gaps) == 4
    assert len({g.ha_trigger_at for g in gaps}) == 4


async def test_a_quiet_camera_produces_no_gap(session_factory, settings) -> None:
    """A quiet house is normal and must never look like a failure."""
    ha = HistoryHA()
    ha.history["binary_sensor.test_motion_detected"] = [(now_utc() - timedelta(hours=2), "off")]

    outcome = (
        await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()
    )[0]

    assert outcome.triggers == 0
    assert await open_gaps(session_factory) == []


async def test_an_image_refresh_without_a_trigger_is_not_a_gap(session_factory, settings) -> None:
    """Measured on real hardware: image entities advance without a detection.

    On 2026-09-09 at 09:03:39 the garage image advanced with no motion, no
    person detection and no automation run. Keying reconciliation off the image
    entity would have invented a delivery gap out of nothing, which is why the
    trigger entity is the signal and the image is never consulted here.
    """
    ha = HistoryHA()
    ha.history["binary_sensor.test_motion_detected"] = [(now_utc() - timedelta(hours=1), "off")]
    # An advancing image entity is present in HA but plays no part.
    ha.history["image.test_event_image"] = [
        (now_utc() - timedelta(minutes=10), "2026-09-09T16:03:39+00:00")
    ]

    await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()
    assert await open_gaps(session_factory) == []


async def test_a_recent_trigger_is_not_yet_declared_missing(session_factory, settings) -> None:
    """A delivery still waiting out the freshness gate has not been lost."""
    ha = HistoryHA()
    fired(ha, "binary_sensor.test_motion_detected", now_utc() - timedelta(seconds=5))

    outcome = (
        await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()
    )[0]

    assert outcome.triggers == 0
    assert await open_gaps(session_factory) == []


async def test_repeated_passes_do_not_duplicate_a_gap(session_factory, settings) -> None:
    """The lookback deliberately overlaps, so the same trigger is seen often."""
    ha = HistoryHA()
    fired(ha, "binary_sensor.test_motion_detected", now_utc() - timedelta(minutes=10))
    rec = reconciler(session_factory, settings, cameras_of(test=camera()), ha)

    for _ in range(4):
        await rec.reconcile_once()

    assert len(await open_gaps(session_factory)) == 1


async def test_a_late_delivery_resolves_a_recorded_gap(session_factory, settings) -> None:
    """Transport can retry. The gap was real, so it is relabelled, not deleted."""
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    fired(ha, "binary_sensor.test_motion_detected", when)
    rec = reconciler(session_factory, settings, cameras_of(test=camera()), ha)

    await rec.reconcile_once()
    assert len(await open_gaps(session_factory)) == 1

    await deliver(session_factory, "test", "image.test_event_image", when)
    outcome = (await rec.reconcile_once())[0]

    assert outcome.resolved == 1
    assert await open_gaps(session_factory) == []
    async with session_scope(session_factory) as session:
        rows = await DeliveryGapRepository(session).gaps_between(
            start=when - timedelta(hours=1), end=now_utc()
        )
    assert rows == []  # no longer open, but the row still exists as history


async def test_cameras_are_reconciled_independently(session_factory, settings) -> None:
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    fired(ha, "binary_sensor.alpha_motion_detected", when)
    fired(ha, "binary_sensor.beta_motion_detected", when)
    await deliver(session_factory, "alpha", "image.alpha_event_image", when)

    cams = cameras_of(
        alpha=camera("alpha", "binary_sensor.alpha_motion_detected"),
        beta=camera("beta", "binary_sensor.beta_motion_detected"),
    )
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    assert await open_gaps(session_factory, "alpha") == []
    assert len(await open_gaps(session_factory, "beta")) == 1


async def test_a_camera_without_a_trigger_entity_is_skipped(session_factory, settings) -> None:
    """Not every camera can be reconciled; that is unknown, not a gap."""
    ha = HistoryHA()
    cams = cameras_of(test=camera(trigger=None))
    outcomes = await reconciler(session_factory, settings, cams, ha).reconcile_once()
    assert outcomes == []
    assert ha.calls == 0


async def test_ha_unreachable_does_not_advance_the_watermark(session_factory, settings) -> None:
    """Not being able to check is not the same as finding nothing wrong."""
    ha = HistoryHA()
    ha.raise_for_all = HomeAssistantError("ha_timeout", "timed out", retryable=True)

    outcome = (
        await reconciler(session_factory, settings, cameras_of(test=camera()), ha).reconcile_once()
    )[0]

    assert outcome.error == "ha_timeout"
    async with session_scope(session_factory) as session:
        assert await DeliveryGapRepository(session).get_state("test") is None


async def test_a_pass_failure_never_stops_the_reconciler(session_factory, settings) -> None:
    class Exploding(HistoryHA):
        async def get_state_history(self, *a, **k):
            raise RuntimeError("malformed history payload")

    rec = reconciler(session_factory, settings, cameras_of(test=camera()), Exploding())
    settings.delivery_reconciliation_interval_seconds = 30
    await rec.start()
    try:
        assert rec.running
    finally:
        await rec.stop()


# --------------------------------------------------------------------------- #
# Pipeline health and historical coverage
# --------------------------------------------------------------------------- #


async def service_for(session, settings, cameras):
    from hermes_home.services.event_service import EventService

    return EventService(session, settings=settings, cameras=cameras)


async def test_quiet_camera_is_healthy_not_unknown(session_factory, settings) -> None:
    """The correction that matters: no traffic is not a lack of health.

    Reconciliation ran and found nothing wrong. The mechanism works; there was
    simply no delivery opportunity. That is `no_recent_trigger`, not a fault
    and not unknown.
    """
    ha = HistoryHA()
    ha.history["binary_sensor.test_motion_detected"] = [(now_utc() - timedelta(hours=1), "off")]
    cams = cameras_of(test=camera())
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        health = await (await service_for(session, settings, cams)).pipeline_health("test")

    assert health.status == PipelineStatus.HEALTHY
    assert health.verification_mode == VerificationMode.NO_RECENT_TRIGGER
    assert health.last_reconciliation_check_at is not None


async def test_a_confirmed_delivery_is_actively_verified(session_factory, settings) -> None:
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    fired(ha, "binary_sensor.test_motion_detected", when)
    await deliver(session_factory, "test", "image.test_event_image", when)
    cams = cameras_of(test=camera())
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        health = await (await service_for(session, settings, cams)).pipeline_health("test")

    assert health.status == PipelineStatus.HEALTHY
    assert health.verification_mode == VerificationMode.ACTIVE
    assert health.last_verified_delivery_at is not None


async def test_a_missing_delivery_degrades_the_pipeline(session_factory, settings) -> None:
    ha = HistoryHA()
    fired(ha, "binary_sensor.test_motion_detected", now_utc() - timedelta(minutes=10))
    cams = cameras_of(test=camera())
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        health = await (await service_for(session, settings, cams)).pipeline_health("test")

    assert health.status == PipelineStatus.DEGRADED
    assert health.open_gap_count == 1
    assert health.reason == "webhook_not_received"


async def test_never_reconciled_is_unknown_not_healthy(session_factory, settings) -> None:
    cams = cameras_of(test=camera())
    async with session_scope(session_factory) as session:
        health = await (await service_for(session, settings, cams)).pipeline_health("test")
    assert health.status == PipelineStatus.UNKNOWN
    assert health.verification_mode == VerificationMode.PASSIVE


async def test_pipeline_coverage_is_true_for_a_quiet_checked_interval(
    session_factory, settings
) -> None:
    """No trigger is required for `true`."""
    ha = HistoryHA()
    ha.history["binary_sensor.test_motion_detected"] = [(now_utc() - timedelta(hours=1), "off")]
    cams = cameras_of(test=camera())
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        view = await (await service_for(session, settings, cams)).pipeline_coverage_for(
            start=now_utc() - timedelta(minutes=20),
            end=now_utc() - timedelta(minutes=5),
            camera="test",
        )
    assert view.complete is True
    assert view.delivery_gaps == []


async def test_pipeline_coverage_is_false_when_a_delivery_is_missing(
    session_factory, settings
) -> None:
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    fired(ha, "binary_sensor.test_motion_detected", when)
    cams = cameras_of(test=camera())
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        view = await (await service_for(session, settings, cams)).pipeline_coverage_for(
            start=now_utc() - timedelta(minutes=30), end=now_utc(), camera="test"
        )
    assert view.complete is False
    assert len(view.delivery_gaps) == 1
    assert view.delivery_gaps[0].reason == "webhook_not_received"
    assert "never received" in view.delivery_gaps[0].meaning


async def test_pipeline_coverage_before_tracking_is_unknown(session_factory, settings) -> None:
    """No gap rows for last month because nobody was reconciling last month."""
    ha = HistoryHA()
    ha.history["binary_sensor.test_motion_detected"] = [(now_utc() - timedelta(hours=1), "off")]
    cams = cameras_of(test=camera())
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        view = await (await service_for(session, settings, cams)).pipeline_coverage_for(
            start=now_utc() - timedelta(days=30),
            end=now_utc() - timedelta(days=29),
            camera="test",
        )
    assert view.complete is None
    assert view.unknown_periods


@pytest.mark.parametrize("missing_for", ["alpha", "beta"])
async def test_zone_pipeline_coverage_reports_the_failing_camera(
    session_factory, settings, missing_for
) -> None:
    ha = HistoryHA()
    when = now_utc() - timedelta(minutes=10)
    for key in ("alpha", "beta"):
        fired(ha, f"binary_sensor.{key}_motion_detected", when)
        if key != missing_for:
            await deliver(session_factory, key, f"image.{key}_event_image", when)

    cams = CamerasConfig(
        cameras={
            "alpha": CameraConfig(
                name="Alpha",
                camera_entity="camera.alpha",
                event_image_entity="image.alpha_event_image",
                trigger_entity="binary_sensor.alpha_motion_detected",
                location="garage_entry",
                observes=["driveway"],
            ),
            "beta": CameraConfig(
                name="Beta",
                camera_entity="camera.beta",
                event_image_entity="image.beta_event_image",
                trigger_entity="binary_sensor.beta_motion_detected",
                location="garage_entry",
                observes=["driveway"],
            ),
        }
    )
    await reconciler(session_factory, settings, cams, ha).reconcile_once()

    async with session_scope(session_factory) as session:
        view = await (await service_for(session, settings, cams)).pipeline_coverage_for(
            start=now_utc() - timedelta(minutes=30), end=now_utc(), zone="driveway"
        )

    # One camera delivering does NOT excuse the other's loss: that event is
    # gone regardless of what the neighbouring camera managed to send.
    assert view.complete is False
    assert [g.camera for g in view.delivery_gaps] == [missing_for]
