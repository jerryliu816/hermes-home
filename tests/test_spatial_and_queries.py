"""Spatial model and temporal query primitives.

These are the read-side foundations the MCP tools will sit on in Milestone 6.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc
from hermes_home.spatial import adjacent_zone_keys, resolve_zone_id, zone_by_key
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Event, Zone
from hermes_home.storage.repositories import EventRepository

# --------------------------------------------------------------------------- #
# Spatial
# --------------------------------------------------------------------------- #


async def test_zones_are_seeded_from_yaml(session_factory) -> None:
    async with session_scope(session_factory) as session:
        zone = await zone_by_key(session, "front_entry")
        assert zone is not None
        assert zone.name == "Front Entry"
        assert zone.kind == "threshold"


async def test_camera_entity_resolves_to_its_zone(session_factory) -> None:
    async with session_scope(session_factory) as session:
        zone_id = await resolve_zone_id(session, "image.front_door_event_image")
        assert zone_id is not None
        assert zone_id == (await zone_by_key(session, "front_entry")).id


async def test_unknown_entity_resolves_to_nothing(session_factory) -> None:
    async with session_scope(session_factory) as session:
        assert await resolve_zone_id(session, "camera.unknown") is None
        assert await resolve_zone_id(session, None) is None


async def test_adjacency_is_bidirectional(session_factory) -> None:
    """Declared once in YAML, stored both ways so either direction queries."""
    async with session_scope(session_factory) as session:
        assert "front_walkway" in await adjacent_zone_keys(session, "driveway")
        assert "driveway" in await adjacent_zone_keys(session, "front_walkway")


async def test_leads_to_relations_are_present(session_factory) -> None:
    async with session_scope(session_factory) as session:
        assert "front_porch" in await adjacent_zone_keys(session, "front_walkway")
        # The driveway meets the garage through its entry, not directly.
        assert "garage_entry" in await adjacent_zone_keys(session, "driveway")
        assert "garage" in await adjacent_zone_keys(session, "garage_entry")


async def test_seeding_is_idempotent(session_factory, home_config, cameras_config) -> None:
    """Startup runs this every time; it must not accumulate duplicates."""
    from sqlalchemy import func, select

    from hermes_home.spatial import seed_home
    from hermes_home.storage.models import Zone, ZoneEdge

    async with session_scope(session_factory) as session:
        before_zones = await session.scalar(select(func.count()).select_from(Zone))
        before_edges = await session.scalar(select(func.count()).select_from(ZoneEdge))

    async with session_scope(session_factory) as session:
        await seed_home(session, home_config, cameras_config)

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Zone)) == before_zones
        assert await session.scalar(select(func.count()).select_from(ZoneEdge)) == before_edges


# --------------------------------------------------------------------------- #
# Temporal queries
# --------------------------------------------------------------------------- #


async def _make_event(session, *, occurred_at, event_type, entity, zone_id, tags=()):
    repo = EventRepository(session)
    event = Event(
        uid=new_uid(),
        delivery_key=new_uid(),
        event_type=event_type,
        source="home_assistant",
        source_entity_id=entity,
        zone_id=zone_id,
        occurred_at=occurred_at,
        received_at=occurred_at,
        payload={},
        payload_schema_version=1,
        created_at=now_utc(),
    )
    await repo.create(event)
    if tags:
        await repo.add_tags(event.id, list(tags), source="vision")
    return event


@pytest.fixture
async def seeded_events(session_factory):
    base = now_utc()
    async with session_scope(session_factory) as session:
        front = (await zone_by_key(session, "front_entry")).id
        drive = (await zone_by_key(session, "driveway")).id
        await _make_event(
            session,
            occurred_at=base - timedelta(minutes=5),
            event_type="camera.person_detected",
            entity="image.front_door_event_image",
            zone_id=front,
            tags=["person_present", "package_present"],
        )
        await _make_event(
            session,
            occurred_at=base - timedelta(hours=3),
            event_type="camera.vehicle_detected",
            entity="camera.driveway",
            zone_id=drive,
            tags=["vehicle_present"],
        )
        await _make_event(
            session,
            occurred_at=base - timedelta(days=2),
            event_type="camera.motion",
            entity="image.front_door_event_image",
            zone_id=front,
        )
    return base


async def test_search_by_time_range(session_factory, seeded_events) -> None:
    base = seeded_events
    async with session_scope(session_factory) as session:
        recent = await EventRepository(session).search(start=base - timedelta(hours=1))
        assert len(recent) == 1

        today = await EventRepository(session).search(start=base - timedelta(hours=6))
        assert len(today) == 2


async def test_results_are_newest_first(session_factory, seeded_events) -> None:
    async with session_scope(session_factory) as session:
        events = await EventRepository(session).search()
        times = [e.occurred_at for e in events]
        assert times == sorted(times, reverse=True)


async def test_filter_by_event_type_prefix(session_factory, seeded_events) -> None:
    """Dotted namespaces make 'everything from cameras' a prefix query."""
    async with session_scope(session_factory) as session:
        repo = EventRepository(session)
        assert len(await repo.search(event_type_prefix="camera.")) == 3
        assert len(await repo.search(event_type_prefix="camera.person")) == 1
        assert len(await repo.search(event_type_prefix="energy.")) == 0


async def test_filter_by_zone(session_factory, seeded_events) -> None:
    async with session_scope(session_factory) as session:
        front = (await zone_by_key(session, "front_entry")).id
        assert len(await EventRepository(session).search(zone_id=front)) == 2


async def test_filter_by_camera_entity(session_factory, seeded_events) -> None:
    async with session_scope(session_factory) as session:
        found = await EventRepository(session).search(source_entity_id="camera.driveway")
        assert len(found) == 1
        assert found[0].event_type == "camera.vehicle_detected"


async def test_filter_by_tag_spans_event_types(session_factory, seeded_events) -> None:
    """The tag table is what lets one filter work across every future event type."""
    async with session_scope(session_factory) as session:
        repo = EventRepository(session)
        assert len(await repo.search(tags=["person_present"])) == 1
        assert len(await repo.search(tags=["vehicle_present"])) == 1
        # Multiple tags are an AND.
        assert len(await repo.search(tags=["person_present", "package_present"])) == 1
        assert len(await repo.search(tags=["person_present", "vehicle_present"])) == 0


async def test_limit_is_respected(session_factory, seeded_events) -> None:
    async with session_scope(session_factory) as session:
        assert len(await EventRepository(session).search(limit=2)) == 2


# --------------------------------------------------------------------------- #
# Milestone 5: roles, observation, and convergent seeding
# --------------------------------------------------------------------------- #


async def test_camera_is_located_in_one_zone_but_observes_several(
    session_factory, cameras_config
) -> None:
    """The distinction the composite primary key exists to express."""
    from hermes_home.spatial import entities_observing

    async with session_scope(session_factory) as session:
        located = await resolve_zone_id(session, "image.front_door_event_image")
        front_entry = (await zone_by_key(session, "front_entry")).id
        assert located == front_entry

        # ...and it can see into the porch and walkway, which are other zones.
        assert "image.front_door_event_image" in await entities_observing(session, "front_porch")
        assert "image.front_door_event_image" in await entities_observing(session, "front_walkway")


async def test_observing_a_zone_no_camera_watches_returns_nothing(
    session_factory,
) -> None:
    from hermes_home.spatial import entities_observing

    async with session_scope(session_factory) as session:
        assert await entities_observing(session, "backyard") == []
        assert await entities_observing(session, "no_such_zone") == []


def test_cameras_observing_reads_config(cameras_config) -> None:
    from hermes_home.spatial import cameras_observing, zones_covered_by

    assert cameras_observing(cameras_config, "front_porch") == ["front_door"]
    assert cameras_observing(cameras_config, "front_entry") == ["front_door"]
    # Two cameras on the garage's front wall both watch the driveway.
    assert cameras_observing(cameras_config, "driveway") == ["garage_left", "garage_right"]
    # Nothing looks inside the garage or at the street.
    assert cameras_observing(cameras_config, "garage") == []
    assert cameras_observing(cameras_config, "street") == []

    covered = zones_covered_by(cameras_config)
    assert {"front_entry", "front_porch", "front_walkway", "backyard", "cottage"} <= covered
    assert "garage" not in covered, "an unobserved zone must not look covered"
    assert "street" not in covered


async def test_zone_relations_carry_their_relation_type(session_factory) -> None:
    from hermes_home.spatial import zone_relations

    async with session_scope(session_factory) as session:
        relations = await zone_relations(session, "front_walkway")

    by_target = {r["to"]: r["relation"] for r in relations}
    assert by_target["front_porch"] == "leads_to"
    assert by_target["driveway"] == "adjacent"


async def test_seeding_converges_when_config_shrinks(
    session_factory, home_config, cameras_config
) -> None:
    """Startup re-seeds every time, so removals must actually remove.

    Otherwise a camera deleted from the config keeps resolving events to a zone
    it no longer watches.
    """
    from hermes_home.config import CamerasConfig
    from hermes_home.spatial import seed_home
    from hermes_home.storage.models import EntityZone

    async with session_scope(session_factory) as session:
        before = await session.scalar(select(func.count()).select_from(EntityZone))
        assert before > 0

    async with session_scope(session_factory) as session:
        await seed_home(session, home_config, CamerasConfig(cameras={}))

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(EntityZone)) == 0
        # Zones themselves survive; only the camera relationships went.
        assert await session.scalar(select(func.count()).select_from(Zone)) > 0


async def test_seeding_converges_when_relationships_change(
    session_factory, home_config, cameras_config
) -> None:
    from hermes_home.spatial import adjacent_zone_keys, seed_home
    from hermes_home.storage.models import ZoneEdge

    trimmed = home_config.model_copy(deep=True)
    trimmed.relationships = [
        r for r in trimmed.relationships if r.from_zone != "driveway" or r.to_zone != "garage_entry"
    ]

    async with session_scope(session_factory) as session:
        await seed_home(session, trimmed, cameras_config)

    async with session_scope(session_factory) as session:
        assert "garage_entry" not in await adjacent_zone_keys(session, "driveway")
        assert await session.scalar(select(func.count()).select_from(ZoneEdge)) > 0
