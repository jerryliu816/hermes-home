"""Two cameras, end to end.

The claim this file defends is that adding a camera is configuration, not code:
nothing in ingestion, storage, spatial resolution or the MCP surface knows which
camera it is dealing with. If any of these need editing to add a third camera,
that claim was false.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from hermes_home.api.deps import AppState
from hermes_home.core.ids import delivery_key
from hermes_home.core.time import now_utc
from hermes_home.ingest.worker import IngestWorker
from hermes_home.spatial import cameras_observing, resolve_zone_id
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Event, Incident, Zone
from hermes_home.storage.repositories import DeliveryRepository
from hermes_home.vision.mock import MockVisionProvider
from tests.conftest import OTHER_IMAGE, TINY_IMAGE, FakeHomeAssistant

CAMERAS = {
    "front_door": {
        "image_entity": "image.front_door_event_image",
        "event_type": "camera.person_detected",
        "zone": "front_entry",
    },
    "garage_right": {
        "image_entity": "image.garage_right_event_image",
        "event_type": "camera.motion",
        "zone": "garage_entry",
    },
}


async def _deliver(session_factory, camera: str, *, occurred_at=None) -> str:
    spec = CAMERAS[camera]
    moment = occurred_at or now_utc()
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=delivery_key(
                source="home_assistant",
                event_type=spec["event_type"],
                source_entity_id=spec["image_entity"],
                occurred_at=moment,
            ),
            correlation_id=f"{camera}-test",
            raw_body={
                "event_type": spec["event_type"],
                "camera": camera,
                "entity_id": spec["image_entity"],
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
        return delivery.uid


def _worker(session_factory, settings, cameras_config, ha, vision=None) -> IngestWorker:
    return IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=ha,
        vision=vision or MockVisionProvider(),
    )


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_both_cameras_are_configured(cameras_config) -> None:
    assert set(cameras_config.cameras) == {"front_door", "garage_right"}
    garage = cameras_config.cameras["garage_right"]
    assert garage.event_image_entity == "image.garage_right_event_image"
    assert garage.camera_entity == "camera.garage_right"
    assert garage.event_image_strategy == "image_entity_state"
    assert garage.location == "garage_entry"


def test_garage_cameras_do_not_claim_to_see_inside_the_garage(cameras_config) -> None:
    """They are mounted on the garage's outward face, watching the driveway.

    Claiming otherwise would make an absence of garage events read as an
    all-clear for a space nothing actually watches.
    """
    assert cameras_config.cameras["garage_right"].observes == ["driveway"]
    assert cameras_observing(cameras_config, "driveway") == ["garage_right"]
    assert cameras_observing(cameras_config, "garage") == []


async def test_each_camera_owns_its_zone(session_factory) -> None:
    """Distinct cameras resolve to distinct places."""
    async with session_scope(session_factory) as session:
        front = await resolve_zone_id(session, "image.front_door_event_image")
        garage = await resolve_zone_id(session, "image.garage_right_event_image")
        assert front is not None and garage is not None
        assert front != garage
        assert (await session.get(Zone, front)).key == "front_entry"
        assert (await session.get(Zone, garage)).key == "garage_entry"


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


async def test_both_cameras_ingest_through_the_same_pipeline(
    session_factory, settings, cameras_config
) -> None:
    ha = FakeHomeAssistant(image_state_ts=now_utc() + timedelta(seconds=5))
    await _deliver(session_factory, "front_door")
    assert await _worker(session_factory, settings, cameras_config, ha).drain_once()

    ha.image = OTHER_IMAGE
    ha.image_state_ts = now_utc() + timedelta(seconds=30)
    await _deliver(session_factory, "garage_right", occurred_at=now_utc() + timedelta(seconds=20))
    assert await _worker(session_factory, settings, cameras_config, ha).drain_once()

    async with session_scope(session_factory) as session:
        events = list((await session.scalars(select(Event).order_by(Event.id))).all())
        assert len(events) == 2
        assert events[0].source_entity_id == "image.front_door_event_image"
        assert events[1].source_entity_id == "image.garage_right_event_image"
        assert events[0].event_type == "camera.person_detected"
        assert events[1].event_type == "camera.motion"
        assert events[0].zone_id != events[1].zone_id


async def test_simultaneous_events_from_both_cameras_stay_separate(
    session_factory, settings, cameras_config
) -> None:
    """Same instant, same bytes, different cameras: two events, not a duplicate.

    Content dedupe is scoped per source entity, so an identical frame from a
    different camera must not be suppressed.
    """
    moment = now_utc()
    ha = FakeHomeAssistant(image=TINY_IMAGE, image_state_ts=moment + timedelta(seconds=5))

    await _deliver(session_factory, "front_door", occurred_at=moment)
    await _deliver(session_factory, "garage_right", occurred_at=moment)
    worker = _worker(session_factory, settings, cameras_config, ha)
    assert await worker.drain_once()
    assert await worker.drain_once()

    async with session_scope(session_factory) as session:
        events = list((await session.scalars(select(Event))).all())
        assert len(events) == 2, "identical bytes from different cameras are not duplicates"
        assert {e.source_entity_id for e in events} == {
            "image.front_door_event_image",
            "image.garage_right_event_image",
        }


async def test_cameras_do_not_share_incidents(session_factory, settings, cameras_config) -> None:
    """Correlation is per zone, so two cameras in different zones never merge."""
    moment = now_utc()
    ha = FakeHomeAssistant(image_state_ts=moment + timedelta(seconds=5))

    await _deliver(session_factory, "front_door", occurred_at=moment)
    await _deliver(session_factory, "garage_right", occurred_at=moment + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)
    await worker.drain_once()
    ha.image = OTHER_IMAGE
    ha.image_state_ts = moment + timedelta(seconds=30)
    await worker.drain_once()

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Incident)) == 2
        events = list((await session.scalars(select(Event))).all())
        assert len({e.incident_id for e in events}) == 2


async def test_one_camera_failing_does_not_affect_the_other(
    session_factory, settings, cameras_config
) -> None:
    """A stale frame on one camera must not stop the other being recorded."""
    moment = now_utc()
    ha = FakeHomeAssistant(image_state_ts=moment + timedelta(seconds=5))

    await _deliver(session_factory, "front_door", occurred_at=moment)
    worker = _worker(session_factory, settings, cameras_config, ha)
    await worker.drain_once()

    # Garage triggers, but its event still never advances past the trigger.
    ha.image_state_ts = moment - timedelta(minutes=5)
    settings.freshness_poll_attempts = 2
    settings.freshness_poll_interval_seconds = 0.001
    await _deliver(session_factory, "garage_right", occurred_at=moment + timedelta(seconds=30))
    await worker.drain_once()

    async with session_scope(session_factory) as session:
        events = list((await session.scalars(select(Event))).all())
        assert len(events) == 1
        assert events[0].source_entity_id == "image.front_door_event_image"


# --------------------------------------------------------------------------- #
# Query surface
# --------------------------------------------------------------------------- #


@pytest.fixture
async def two_camera_events(session_factory, settings, cameras_config):
    base = now_utc() - timedelta(minutes=10)
    ha = FakeHomeAssistant(image_state_ts=base + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)

    await _deliver(session_factory, "front_door", occurred_at=base)
    await worker.drain_once()
    ha.image = OTHER_IMAGE
    ha.image_state_ts = base + timedelta(seconds=60)
    await _deliver(session_factory, "garage_right", occurred_at=base + timedelta(seconds=45))
    await worker.drain_once()
    return base


@pytest.fixture
async def mcp_server(session_factory, settings, home_config, cameras_config, fake_ha):
    from hermes_home.mcp.server import create_mcp_server

    return create_mcp_server(
        AppState(
            settings=settings,
            home=home_config,
            cameras=cameras_config,
            engine=None,
            session_factory=session_factory,
            ha_client=fake_ha,
            vision=MockVisionProvider(),
            worker=None,
        )
    )


async def _call(mcp_server, name, args=None):
    result = await mcp_server.call_tool(name, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_mcp_filters_by_camera(mcp_server, two_camera_events) -> None:
    """The user-facing payoff: 'what happened at the garage' must not return
    front door events."""
    everything = await _call(mcp_server, "home_recent_events")
    assert everything["count"] == 2

    garage = await _call(mcp_server, "home_recent_events", {"camera": "garage_right"})
    assert garage["count"] == 1
    assert garage["events"][0]["camera"] == "garage_right"
    assert garage["events"][0]["zone"] == "garage_entry"

    front = await _call(mcp_server, "home_recent_events", {"camera": "front_door"})
    assert front["count"] == 1
    assert front["events"][0]["camera"] == "front_door"


async def test_mcp_filters_by_zone(mcp_server, two_camera_events) -> None:
    garage = await _call(mcp_server, "home_search_events", {"zone": "garage_entry"})
    assert garage["count"] == 1
    assert garage["events"][0]["camera"] == "garage_right"

    # Nothing observes the garage interior, so nothing is ever recorded there.
    assert (await _call(mcp_server, "home_search_events", {"zone": "garage"}))["count"] == 0


async def test_summary_breaks_down_by_camera_and_zone(mcp_server, two_camera_events) -> None:
    summary = await _call(mcp_server, "home_summarize_activity")

    assert summary["event_count"] == 2
    assert summary["by_camera"] == {"front_door": 1, "garage_right": 1}
    assert summary["by_zone"] == {"front_entry": 1, "garage_entry": 1}
    assert summary["by_type"] == {"camera.person_detected": 1, "camera.motion": 1}


async def test_describe_home_lists_both_cameras_and_their_coverage(mcp_server) -> None:
    home = await _call(mcp_server, "home_describe_home")

    by_key = {c["key"]: c for c in home["cameras"]}
    assert set(by_key) == {"front_door", "garage_right"}
    assert by_key["garage_right"]["located_in"] == "garage_entry"
    assert by_key["garage_right"]["observes"] == ["driveway"]
    assert "garage" in home["unobserved_zones"]
