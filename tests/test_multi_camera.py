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

#: Every camera in the fixture config, which mirrors the real deployment.
ALL_CAMERAS = {
    "backyard",
    "cottage",
    "front_door",
    "garage_left",
    "garage_right",
    "left_walkway",
    "right_walkway",
}

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
    # Entity IDs deliberately keep their pre-rename names.
    "garage_left": {
        "image_entity": "image.driveway_event_image",
        "event_type": "camera.motion",
        "zone": "garage_entry",
    },
    "left_walkway": {
        "image_entity": "image.left_side_door_event_image",
        "event_type": "camera.motion",
        "zone": "left_walkway",
    },
    "right_walkway": {
        "image_entity": "image.right_walkway_event_image",
        "event_type": "camera.motion",
        "zone": "right_walkway",
    },
    "backyard": {
        "image_entity": "image.backyard_event_image",
        "event_type": "camera.motion",
        "zone": "backyard",
    },
    "cottage": {
        "image_entity": "image.cottage_event_image",
        "event_type": "camera.motion",
        "zone": "cottage",
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


def _png_variant(index: int) -> bytes:
    """A distinct but valid image per camera, so nothing is deduped by content."""
    from tests.conftest import _png

    return _png(4, 3, tag=f"camera-{index}".encode())


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


def test_every_camera_is_configured(cameras_config) -> None:
    assert set(cameras_config.cameras) == {
        "backyard",
        "cottage",
        "front_door",
        "garage_left",
        "garage_right",
        "left_walkway",
        "right_walkway",
    }

    garage = cameras_config.cameras["garage_right"]
    assert garage.event_image_entity == "image.garage_right_event_image"
    assert garage.camera_entity == "camera.garage_right"
    assert garage.event_image_strategy == "image_entity_state"
    assert garage.location == "garage_entry"


def test_every_camera_uses_the_same_retrieval_strategy(cameras_config) -> None:
    """Nothing is special-cased: every camera goes through one code path."""
    for key, camera in cameras_config.cameras.items():
        assert camera.event_image_strategy == "image_entity_state", key
        assert camera.event_image_entity, key
        assert camera.camera_entity, key


def test_entity_ids_are_unique_across_cameras(cameras_config) -> None:
    """Two cameras sharing an entity would silently merge their histories."""
    images = [c.event_image_entity for c in cameras_config.cameras.values()]
    assert len(images) == len(set(images))
    cams = [c.camera_entity for c in cameras_config.cameras.values()]
    assert len(cams) == len(set(cams))


def test_renamed_cameras_keep_their_original_entity_ids(cameras_config) -> None:
    """Home Assistant entity IDs outlive the names people use.

    garage_left and left_walkway were renamed; their entities were not. Aliases
    carry the old names so a question phrased the old way still resolves.
    """
    garage_left = cameras_config.cameras["garage_left"]
    assert garage_left.camera_entity == "camera.driveway"
    assert garage_left.event_image_entity == "image.driveway_event_image"
    assert "driveway" in garage_left.aliases

    left = cameras_config.cameras["left_walkway"]
    assert left.camera_entity == "camera.left_side_door"
    assert left.event_image_entity == "image.left_side_door_event_image"
    assert "left side door" in left.aliases


def test_garage_cameras_do_not_claim_to_see_inside_the_garage(cameras_config) -> None:
    """Both are mounted on the garage's outward face, watching the driveway.

    Claiming otherwise would make an absence of garage events read as an
    all-clear for a space nothing actually watches.
    """
    assert cameras_config.cameras["garage_right"].observes == ["driveway"]
    assert cameras_config.cameras["garage_left"].observes == ["driveway"]
    assert cameras_observing(cameras_config, "driveway") == ["garage_left", "garage_right"]
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


async def test_describe_home_lists_every_camera_and_its_coverage(mcp_server) -> None:
    home = await _call(mcp_server, "home_describe_home")

    by_key = {c["key"]: c for c in home["cameras"]}
    assert set(by_key) == {
        "backyard",
        "cottage",
        "front_door",
        "garage_left",
        "garage_right",
        "left_walkway",
        "right_walkway",
    }
    assert by_key["garage_right"]["located_in"] == "garage_entry"
    assert by_key["garage_right"]["observes"] == ["driveway"]
    assert by_key["backyard"]["located_in"] == "backyard"
    assert by_key["cottage"]["located_in"] == "cottage"
    assert "garage" in home["unobserved_zones"]


async def test_describe_home_exposes_aliases(mcp_server) -> None:
    """So a question about "the driveway camera" can reach garage_left."""
    home = await _call(mcp_server, "home_describe_home")
    by_key = {c["key"]: c for c in home["cameras"]}

    assert "driveway" in by_key["garage_left"]["aliases"]
    assert "left side door" in by_key["left_walkway"]["aliases"]
    assert by_key["front_door"]["aliases"] == []


# --------------------------------------------------------------------------- #
# Properties that only appear once there are many cameras
# --------------------------------------------------------------------------- #


async def test_every_camera_ingests_and_lands_in_its_own_zone(
    session_factory, settings, cameras_config
) -> None:
    """The whole fleet through one code path, each event attributable."""
    base = now_utc() - timedelta(minutes=30)
    ha = FakeHomeAssistant(image_state_ts=base + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)

    for index, camera in enumerate(sorted(CAMERAS)):
        # A distinct frame per camera, so nothing is suppressed as duplicate.
        ha.image = _png_variant(index)
        ha.image_state_ts = base + timedelta(minutes=index, seconds=5)
        await _deliver(session_factory, camera, occurred_at=base + timedelta(minutes=index))
        assert await worker.drain_once(), camera

    async with session_scope(session_factory) as session:
        rows = (
            await session.execute(
                select(Event.source_entity_id, Zone.key).join(Zone, Zone.id == Event.zone_id)
            )
        ).all()

    by_entity = dict(rows)
    assert len(by_entity) == len(CAMERAS), "every camera produced its own event"
    for camera, spec in CAMERAS.items():
        assert by_entity[spec["image_entity"]] == spec["zone"], camera


async def test_two_cameras_in_one_zone_share_an_incident(
    session_factory, settings, cameras_config
) -> None:
    """garage_left and garage_right both watch the driveway from the same wall.

    One person crossing it trips both, and that is one occurrence, not two --
    which is exactly what an incident is for. The *events* stay separate; only
    the incident groups them.
    """
    base = now_utc() - timedelta(minutes=5)
    ha = FakeHomeAssistant(image_state_ts=base + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)

    await _deliver(session_factory, "garage_right", occurred_at=base)
    await worker.drain_once()

    ha.image = OTHER_IMAGE
    ha.image_state_ts = base + timedelta(seconds=40)
    await _deliver(session_factory, "garage_left", occurred_at=base + timedelta(seconds=20))
    await worker.drain_once()

    async with session_scope(session_factory) as session:
        events = list((await session.scalars(select(Event))).all())
        assert len(events) == 2, "two cameras, two distinct events"
        assert len({e.source_entity_id for e in events}) == 2
        assert await session.scalar(select(func.count()).select_from(Incident)) == 1
        assert len({e.incident_id for e in events}) == 1


async def test_cameras_in_different_zones_never_share_an_incident(
    session_factory, settings, cameras_config
) -> None:
    """Overlapping views do not merge: the two shed cameras hold distinct zones."""
    base = now_utc() - timedelta(minutes=5)
    ha = FakeHomeAssistant(image_state_ts=base + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)

    await _deliver(session_factory, "backyard", occurred_at=base)
    await worker.drain_once()

    ha.image = OTHER_IMAGE
    ha.image_state_ts = base + timedelta(seconds=40)
    await _deliver(session_factory, "cottage", occurred_at=base + timedelta(seconds=10))
    await worker.drain_once()

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Incident)) == 2
        events = list((await session.scalars(select(Event))).all())
        assert len({e.incident_id for e in events}) == 2


async def test_identical_frames_across_all_cameras_are_not_duplicates(
    session_factory, settings, cameras_config
) -> None:
    """Content dedupe is per source entity, so a shared frame is not suppressed."""
    base = now_utc() - timedelta(minutes=20)
    ha = FakeHomeAssistant(image=TINY_IMAGE, image_state_ts=base + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)

    for index, camera in enumerate(sorted(CAMERAS)):
        ha.image_state_ts = base + timedelta(minutes=index, seconds=5)
        await _deliver(session_factory, camera, occurred_at=base + timedelta(minutes=index))
        await worker.drain_once()

    async with session_scope(session_factory) as session:
        events = list((await session.scalars(select(Event))).all())

    assert len(events) == len(CAMERAS), "the same bytes from different cameras are distinct"
    assert all(e.duplicate_count == 0 for e in events)


async def test_mcp_filters_by_every_camera_key(
    mcp_server, session_factory, settings, cameras_config
) -> None:
    """The logical keys, not the Home Assistant entity names, are the interface."""
    base = now_utc() - timedelta(minutes=30)
    ha = FakeHomeAssistant(image_state_ts=base + timedelta(seconds=5))
    worker = _worker(session_factory, settings, cameras_config, ha)

    for index, camera in enumerate(sorted(CAMERAS)):
        ha.image = _png_variant(index)
        ha.image_state_ts = base + timedelta(minutes=index, seconds=5)
        await _deliver(session_factory, camera, occurred_at=base + timedelta(minutes=index))
        await worker.drain_once()

    for camera in CAMERAS:
        result = await _call(mcp_server, "home_recent_events", {"camera": camera})
        assert result["count"] == 1, camera
        assert result["events"][0]["camera"] == camera

    summary = await _call(mcp_server, "home_summarize_activity")
    assert summary["by_camera"] == dict.fromkeys(CAMERAS, 1)


# --------------------------------------------------------------------------- #
# Partial coverage
#
# Coverage was binary: watched or not. A camera that sees only the half of the
# yard nearest the house made "the backyard is covered" true and misleading at
# once -- the same over-claim as calling an unwatched zone quiet, one level
# subtler.
# --------------------------------------------------------------------------- #


def test_backyard_is_covered_but_only_partly(cameras_config) -> None:
    from hermes_home.spatial import zones_covered_by, zones_partially_covered_by

    backyard = cameras_config.cameras["backyard"]
    assert "backyard" in backyard.observes, "coverage must be legible on the camera too"
    assert backyard.partial_coverage == ["backyard"]

    assert "backyard" in zones_covered_by(cameras_config)
    assert "backyard" in zones_partially_covered_by(cameras_config)


def test_fully_covered_zones_are_not_reported_as_partial(cameras_config) -> None:
    from hermes_home.spatial import zones_partially_covered_by

    partial = zones_partially_covered_by(cameras_config)
    assert "front_entry" not in partial
    assert "driveway" not in partial
    assert "rear_entry" not in partial, "the sliding doors are fully in frame"


def test_full_coverage_by_another_camera_wins(cameras_config) -> None:
    """If one camera sees only part of a zone but another sees all of it, the
    zone is not partially covered -- otherwise adding a camera would make the
    answer more pessimistic."""
    from hermes_home.config import CamerasConfig
    from hermes_home.spatial import zones_partially_covered_by

    partial_only = CamerasConfig(cameras={"backyard": cameras_config.cameras["backyard"]})
    assert "backyard" in zones_partially_covered_by(partial_only)

    both = CamerasConfig(
        cameras={
            "backyard": cameras_config.cameras["backyard"],
            # A second camera covering the whole yard.
            "yard_wide": cameras_config.cameras["cottage"].model_copy(
                update={"location": "backyard", "observes": [], "partial_coverage": []}
            ),
        }
    )
    assert "backyard" not in zones_partially_covered_by(both)


def test_partial_coverage_must_name_a_zone_the_camera_covers(home_config) -> None:
    """A typo here would silently claim nothing, so it fails at startup."""
    from hermes_home.config import CameraConfig, CamerasConfig, validate_home_and_cameras
    from hermes_home.core.errors import ConfigError

    bogus = CamerasConfig(
        cameras={
            "b": CameraConfig(
                name="B",
                event_image_entity="image.b",
                location="backyard",
                observes=[],
                partial_coverage=["driveway"],  # not covered by this camera at all
            )
        }
    )
    with pytest.raises(ConfigError, match="does not cover it"):
        validate_home_and_cameras(home_config, bogus)


async def test_describe_home_reports_partial_coverage(mcp_server) -> None:
    home = await _call(mcp_server, "home_describe_home")

    assert home["partially_observed_zones"] == ["backyard"]
    assert "backyard" not in home["unobserved_zones"], "partly watched is not unwatched"
    assert "garage" in home["unobserved_zones"]

    by_key = {c["key"]: c for c in home["cameras"]}
    assert by_key["backyard"]["partial_coverage"] == ["backyard"]
    assert by_key["front_door"]["partial_coverage"] == []


async def test_query_results_carry_zone_coverage(mcp_server) -> None:
    """The caveat has to travel with the answer.

    An agent asked "did anything happen in the backyard" calls a query tool and
    never thinks to ask about coverage separately, so partial coverage stated
    only in home_describe_home goes unread.
    """
    partial = await _call(mcp_server, "home_recent_events", {"zone": "backyard"})
    assert partial["zone_coverage"]["status"] == "partial"
    assert "weaker evidence" in partial["zone_coverage"]["note"]

    none = await _call(mcp_server, "home_recent_events", {"zone": "garage"})
    assert none["zone_coverage"]["status"] == "none"
    assert "says nothing" in none["zone_coverage"]["note"]

    full = await _call(mcp_server, "home_search_events", {"zone": "front_entry"})
    assert full["zone_coverage"]["status"] == "full"


async def test_no_zone_filter_means_no_coverage_note(mcp_server) -> None:
    """Nothing to qualify when the question was not about a place."""
    assert "zone_coverage" not in await _call(mcp_server, "home_recent_events")


def test_coverage_of_classifies_every_zone(cameras_config) -> None:
    from hermes_home.spatial import coverage_of

    assert coverage_of(cameras_config, "backyard") == "partial"
    assert coverage_of(cameras_config, "front_entry") == "full"
    assert coverage_of(cameras_config, "driveway") == "full"
    assert coverage_of(cameras_config, "garage") == "none"
    assert coverage_of(cameras_config, "street") == "none"
