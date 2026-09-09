"""Camera health monitoring.

The organising worry: a health system that guesses wrong is worse than none at
all, because it converts "I don't know" into a confident answer. So most of
these tests are about what must NOT be treated as evidence -- silence, an old
timestamp, a single dropped request, a period nobody was watching.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from hermes_home.clients.home_assistant import EntityState
from hermes_home.config import CameraConfig, CamerasConfig
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.time import now_utc
from hermes_home.health.monitor import CameraHealthMonitor, evaluate
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import CameraHealth, HealthReason, HealthStatus
from hermes_home.storage.repositories import CameraHealthRepository


class ScriptedHA:
    """A Home Assistant whose entities can be set individually."""

    def __init__(self, states: dict[str, str] | None = None) -> None:
        self.states: dict[str, str] = states or {}
        self.missing: set[str] = set()
        self.raise_for_all: Exception | None = None
        self.calls = 0

    async def get_entity_state(self, entity_id: str) -> EntityState:
        self.calls += 1
        if self.raise_for_all is not None:
            raise self.raise_for_all
        if entity_id in self.missing:
            raise HomeAssistantError("ha_not_found", f"no such entity: {entity_id}")
        moment = now_utc()
        return EntityState(
            entity_id=entity_id,
            state=self.states.get(entity_id, moment.isoformat()),
            attributes={},
            last_changed=moment,
            last_updated=moment,
        )

    async def aclose(self) -> None:
        return None


def camera(**overrides) -> CameraConfig:
    base = {
        "name": "Test",
        "camera_entity": "camera.test",
        "event_image_entity": "image.test",
        "event_image_strategy": "image_entity_state",
        "location": "front_entry",
    }
    return CameraConfig(**{**base, **overrides})


def monitor(session_factory, settings, cameras, ha) -> CameraHealthMonitor:
    return CameraHealthMonitor(
        session_factory=session_factory, settings=settings, cameras=cameras, ha_client=ha
    )


def one_camera(**overrides) -> CamerasConfig:
    return CamerasConfig(cameras={"test": camera(**overrides)})


# --------------------------------------------------------------------------- #
# Evaluation rules -- pure, no database, no clock
# --------------------------------------------------------------------------- #


def test_available_camera_is_healthy() -> None:
    obs = evaluate(camera(), health_state="idle", image_state="2026-09-09T02:00:00Z")
    assert obs.status == HealthStatus.HEALTHY
    assert obs.reason is None


def test_unavailable_camera_is_offline() -> None:
    obs = evaluate(camera(), health_state="unavailable")
    assert obs.status == HealthStatus.OFFLINE
    assert obs.reason == HealthReason.CAMERA_ENTITY_UNAVAILABLE


def test_missing_camera_entity_is_offline() -> None:
    obs = evaluate(camera(), health_state=None, health_missing=True)
    assert obs.status == HealthStatus.OFFLINE
    assert obs.reason == HealthReason.CAMERA_ENTITY_NOT_FOUND


def test_unavailable_image_entity_is_degraded_not_offline() -> None:
    """The camera answers, but could not produce an analyzable frame."""
    obs = evaluate(camera(), health_state="idle", image_state="unavailable")
    assert obs.status == HealthStatus.DEGRADED
    assert obs.reason == HealthReason.EVENT_IMAGE_ENTITY_UNAVAILABLE


def test_image_state_unknown_is_still_healthy() -> None:
    """The ordinary reading after a Home Assistant reload, until the camera fires.

    Calling this degraded would report every restart as a fault.
    """
    assert evaluate(camera(), health_state="idle", image_state="unknown").status == (
        HealthStatus.HEALTHY
    )


def test_an_old_image_timestamp_alone_is_not_a_fault() -> None:
    """A quiet week is not a broken camera."""
    obs = evaluate(
        camera(),
        health_state="idle",
        image_state="2020-01-01T00:00:00Z",
        image_updated_at=now_utc() - timedelta(days=400),
    )
    assert obs.status == HealthStatus.HEALTHY


def test_camera_with_no_entity_to_poll_is_unknown_not_offline() -> None:
    """We were told nothing about it; that is not the camera's failure."""
    obs = evaluate(
        camera(camera_entity=None, event_image_entity=None, event_image_strategy="none"),
        health_state=None,
    )
    assert obs.status == HealthStatus.UNKNOWN
    assert obs.reason == HealthReason.NO_HEALTH_ENTITY


# --------------------------------------------------------------------------- #
# The health_entity seam: availability and state are independent signals
# --------------------------------------------------------------------------- #


def test_default_semantics_are_availability_only() -> None:
    """No predicate configured -> exactly the behaviour that existed before.

    A camera entity sitting at "idle", "recording", or anything else ordinary is
    reachable. Only unavailable and 404 are faults.
    """
    for state in ("idle", "recording", "streaming", "on", "off"):
        assert evaluate(camera(), health_state=state).status == HealthStatus.HEALTHY


def test_alternate_health_entity_matching_a_healthy_state() -> None:
    cam = camera(health_entity="binary_sensor.test_connected", health_healthy_states=["on"])
    assert evaluate(cam, health_state="on", image_state="idle").status == HealthStatus.HEALTHY


def test_alternate_health_entity_in_an_unhealthy_state_is_offline() -> None:
    """The whole reason the predicate exists.

    A connectivity sensor reports disconnection through its state while staying
    perfectly available, so availability-only rules would call this healthy --
    the exact false all-clear the feature exists to prevent.
    """
    cam = camera(health_entity="binary_sensor.test_connected", health_healthy_states=["on"])
    obs = evaluate(cam, health_state="off")
    assert obs.status == HealthStatus.OFFLINE
    assert obs.reason == HealthReason.HEALTH_ENTITY_UNHEALTHY_STATE
    assert obs.camera_state == "off"


def test_unavailable_beats_a_configured_state_predicate() -> None:
    """Availability is never overridden by state semantics."""
    cam = camera(health_entity="binary_sensor.test_connected", health_healthy_states=["off"])
    obs = evaluate(cam, health_state="unavailable")
    assert obs.status == HealthStatus.OFFLINE
    assert obs.reason == HealthReason.CAMERA_ENTITY_UNAVAILABLE


def test_predicate_with_state_unknown_is_unknown_not_offline() -> None:
    """HA's own not-determinable marker is not evidence of disconnection."""
    cam = camera(health_entity="binary_sensor.test_connected", health_healthy_states=["on"])
    obs = evaluate(cam, health_state="unknown")
    assert obs.status == HealthStatus.UNKNOWN
    assert obs.reason == HealthReason.HEALTH_ENTITY_STATE_UNKNOWN


def test_state_comparison_ignores_case_and_padding_only() -> None:
    cam = camera(health_entity="binary_sensor.x", health_healthy_states=["On"])
    assert evaluate(cam, health_state=" on ").status == HealthStatus.HEALTHY
    # No truthiness table: "true" is not "on" unless configured as such.
    assert evaluate(cam, health_state="true").status == HealthStatus.OFFLINE


def test_healthy_states_without_a_health_entity_is_a_config_error() -> None:
    """Otherwise it would silently change how camera_entity is judged."""
    with pytest.raises(ValueError, match="health_healthy_states without health_entity"):
        camera(health_healthy_states=["on"])


# --------------------------------------------------------------------------- #
# Polling and persistence
# --------------------------------------------------------------------------- #


async def test_poll_records_current_and_opens_an_interval(session_factory, settings) -> None:
    mon = monitor(session_factory, settings, one_camera(), ScriptedHA())
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        repo = CameraHealthRepository(session)
        row = await repo.get("test")
        interval = await repo.open_interval("test")

    assert row.status == HealthStatus.HEALTHY
    assert row.last_healthy_at is not None
    assert interval.status == HealthStatus.HEALTHY
    assert interval.ended_at is None


async def test_steady_state_writes_no_extra_interval_rows(session_factory, settings) -> None:
    """A healthy camera must not accumulate a row a minute."""
    mon = monitor(session_factory, settings, one_camera(), ScriptedHA())
    for _ in range(5):
        await mon.poll_once()

    async with session_scope(session_factory) as session:
        rows = await CameraHealthRepository(session).intervals_for(
            "test", start=now_utc() - timedelta(hours=1), end=now_utc() + timedelta(hours=1)
        )
    assert len(rows) == 1


async def test_ha_unreachable_marks_unknown_not_offline(session_factory, settings) -> None:
    """Home Assistant being down is our blindness, not the camera's failure."""
    ha = ScriptedHA()
    ha.raise_for_all = HomeAssistantError("ha_timeout", "timed out", retryable=True)
    mon = monitor(session_factory, settings, one_camera(), ha)
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        interval = await CameraHealthRepository(session).open_interval("test")
    assert interval.status == HealthStatus.UNKNOWN
    assert interval.reason == HealthReason.HA_UNREACHABLE


async def test_ha_recovery_returns_to_healthy(session_factory, settings) -> None:
    ha = ScriptedHA()
    ha.raise_for_all = HomeAssistantError("ha_connection", "no route", retryable=True)
    mon = monitor(session_factory, settings, one_camera(), ha)
    await mon.poll_once()
    ha.raise_for_all = None
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        row = await CameraHealthRepository(session).get("test")
    assert row.status == HealthStatus.HEALTHY


async def test_healthy_to_offline_and_back(session_factory, settings) -> None:
    ha = ScriptedHA()
    settings.camera_health_failure_threshold = 1
    mon = monitor(session_factory, settings, one_camera(), ha)
    await mon.poll_once()
    ha.states["camera.test"] = "unavailable"
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        row = await CameraHealthRepository(session).get("test")
    assert row.status == HealthStatus.OFFLINE
    assert row.offline_since is not None

    ha.states.pop("camera.test")
    await mon.poll_once()
    async with session_scope(session_factory) as session:
        repo = CameraHealthRepository(session)
        row = await repo.get("test")
        intervals = await repo.intervals_for(
            "test", start=now_utc() - timedelta(hours=1), end=now_utc() + timedelta(hours=1)
        )
    assert row.status == HealthStatus.HEALTHY
    assert row.offline_since is None
    assert [i.status for i in intervals] == [
        HealthStatus.HEALTHY,
        HealthStatus.OFFLINE,
        HealthStatus.HEALTHY,
    ]


async def test_threshold_debounces_current_status_only(session_factory, settings) -> None:
    """One dropped request must not announce an outage...

    ...but it must still be recorded. Debouncing the history would let a real
    observed failure vanish, which is the one thing this must never do.
    """
    settings.camera_health_failure_threshold = 2
    ha = ScriptedHA()
    mon = monitor(session_factory, settings, one_camera(), ha)
    await mon.poll_once()

    ha.states["camera.test"] = "unavailable"
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        repo = CameraHealthRepository(session)
        row = await repo.get("test")
        interval = await repo.open_interval("test")

    # Current status has not flipped yet: one bad poll is not an announcement.
    assert row.status == HealthStatus.HEALTHY
    assert row.pending_status == HealthStatus.OFFLINE
    assert row.observed_status == HealthStatus.OFFLINE
    # But history recorded it immediately.
    assert interval.status == HealthStatus.OFFLINE


async def test_threshold_commits_after_repeated_failures(session_factory, settings) -> None:
    settings.camera_health_failure_threshold = 2
    ha = ScriptedHA(states={"camera.test": "unavailable"})
    mon = monitor(session_factory, settings, one_camera(), ha)
    await mon.poll_once()
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        row = await CameraHealthRepository(session).get("test")
    assert row.status == HealthStatus.OFFLINE


async def test_several_cameras_hold_different_statuses(
    session_factory, settings, cameras_config
) -> None:
    ha = ScriptedHA(
        states={
            "camera.backyard": "unavailable",
            "image.cottage_event_image": "unavailable",
        }
    )
    settings.camera_health_failure_threshold = 1
    mon = monitor(session_factory, settings, cameras_config, ha)
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        rows = {r.camera_key: r.status for r in await CameraHealthRepository(session).all_current()}

    assert rows["backyard"] == HealthStatus.OFFLINE
    assert rows["cottage"] == HealthStatus.DEGRADED
    assert rows["front_door"] == HealthStatus.HEALTHY
    assert len(rows) == len(cameras_config.cameras)


async def test_monitor_runs_without_the_ingest_worker(session_factory, settings) -> None:
    """Health must keep being observed while ingestion is idle."""
    mon = monitor(session_factory, settings, one_camera(), ScriptedHA())
    settings.camera_health_interval_seconds = 5
    await mon.start()
    try:
        assert mon.running
        for _ in range(200):
            await asyncio.sleep(0.01)
            async with session_scope(session_factory) as session:
                if await CameraHealthRepository(session).get("test") is not None:
                    break
        else:  # pragma: no cover - the loop should always find it
            pytest.fail("monitor never recorded a poll")
    finally:
        await mon.stop()
    assert not mon.running


async def test_a_poll_failure_does_not_kill_the_monitor(session_factory, settings) -> None:
    """A bug or a malformed payload costs one interval, not the service."""

    class Exploding(ScriptedHA):
        async def get_entity_state(self, entity_id: str):
            raise RuntimeError("malformed payload")

    mon = monitor(session_factory, settings, one_camera(), Exploding())
    settings.camera_health_interval_seconds = 5
    await mon.start()
    try:
        # The loop swallows it; the task stays alive.
        assert mon.running
    finally:
        await mon.stop()


async def test_a_row_that_has_gone_stale_reports_unknown(
    session_factory, settings, cameras_config
) -> None:
    """A dead monitor must not keep asserting health by no longer writing."""
    from hermes_home.services.event_service import EventService

    stale = now_utc() - timedelta(hours=6)
    async with session_scope(session_factory) as session:
        session.add(
            CameraHealth(
                camera_key="front_door",
                status=HealthStatus.HEALTHY,
                observed_status=HealthStatus.HEALTHY,
                reason=None,
                checked_at=stale,
                last_healthy_at=stale,
                consecutive_failures=0,
                first_observed_at=stale,
            )
        )

    async with session_scope(session_factory) as session:
        views = await EventService(
            session, settings=settings, cameras=cameras_config
        ).camera_health("front_door")

    assert views[0].status == HealthStatus.UNKNOWN
    assert views[0].reason == HealthReason.DATA_STALE
    # The raw value stays visible so the staleness is inspectable.
    assert views[0].persisted_status == HealthStatus.HEALTHY


async def test_disabled_monitoring_reports_unknown(
    session_factory, settings, cameras_config
) -> None:
    from hermes_home.services.event_service import EventService

    settings.camera_health_enabled = False
    async with session_scope(session_factory) as session:
        views = await EventService(
            session, settings=settings, cameras=cameras_config
        ).camera_health("front_door")

    assert views[0].status == HealthStatus.UNKNOWN
    assert views[0].reason == HealthReason.MONITORING_DISABLED
