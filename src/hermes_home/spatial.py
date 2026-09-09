"""Zones, what is in them, and what can see them.

Deliberately one module, not a package. The zone *schema* is first-class in v1 --
zones, edges, and entity relationships all exist and are populated -- and the
queries over it are deterministic set lookups. There is no path-finding engine,
no field-of-view geometry, no trajectory estimation, and no identity tracking.

``config/home.yaml`` is the source of truth for a house you edit by hand; these
tables are the copy that SQL joins and MCP tools can see.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_home.config import CameraConfig, CamerasConfig, HomeConfig
from hermes_home.storage.models import EntityZone, Zone, ZoneEdge

logger = structlog.get_logger(__name__)

#: An entity physically sits in this zone. Exactly one per entity, and it is what
#: resolves an incoming event to a place.
ROLE_LOCATED_IN = "located_in"
#: An entity can see into this zone. Several per entity.
ROLE_OBSERVES = "observes"


async def seed_home(
    session: AsyncSession, home: HomeConfig, cameras: CamerasConfig
) -> dict[str, int]:
    """Upsert zones, edges, and entity relationships. Idempotent.

    Runs on every startup, so it must converge rather than accumulate: removing a
    camera from the config removes its rows here too.
    """
    zone_ids = await _seed_zones(session, home)
    await _seed_edges(session, home, zone_ids)
    await _seed_entities(session, cameras, zone_ids)
    await session.flush()

    logger.info(
        "spatial.seeded",
        zones=len(zone_ids),
        edges=len(home.relationships),
        cameras=len(cameras.cameras),
    )
    return zone_ids


async def _seed_zones(session: AsyncSession, home: HomeConfig) -> dict[str, int]:
    existing = {z.key: z for z in (await session.scalars(select(Zone))).all()}
    zone_ids: dict[str, int] = {}

    for key, cfg in home.zones.items():
        zone = existing.get(key)
        if zone is None:
            zone = Zone(key=key, name=cfg.name, kind=cfg.kind, attributes=cfg.attributes)
            session.add(zone)
        else:
            zone.name, zone.kind, zone.attributes = cfg.name, cfg.kind, cfg.attributes
        await session.flush()
        zone_ids[key] = zone.id

    # Parents in a second pass so the YAML may reference zones declared later.
    for key, cfg in home.zones.items():
        if cfg.parent:
            zone = await session.get(Zone, zone_ids[key])
            if zone is not None:
                zone.parent_zone_id = zone_ids.get(cfg.parent)

    return zone_ids


async def _seed_edges(session: AsyncSession, home: HomeConfig, zone_ids: dict[str, int]) -> None:
    wanted: set[tuple[int, int, str]] = set()
    for rel in home.relationships:
        wanted.add((zone_ids[rel.from_zone], zone_ids[rel.to_zone], rel.relation))
        if rel.bidirectional:
            wanted.add((zone_ids[rel.to_zone], zone_ids[rel.from_zone], rel.relation))

    for edge in (await session.scalars(select(ZoneEdge))).all():
        key = (edge.from_zone_id, edge.to_zone_id, edge.relation)
        if key in wanted:
            wanted.discard(key)
        else:
            await session.delete(edge)  # dropped from config

    for from_id, to_id, relation in wanted:
        session.add(ZoneEdge(from_zone_id=from_id, to_zone_id=to_id, relation=relation))


async def _seed_entities(
    session: AsyncSession, cameras: CamerasConfig, zone_ids: dict[str, int]
) -> None:
    wanted: set[tuple[str, int, str]] = set()
    for camera in cameras.cameras.values():
        for entity_id in _entities_of(camera):
            wanted.add((entity_id, zone_ids[camera.location], ROLE_LOCATED_IN))
            for observed in camera.observes:
                wanted.add((entity_id, zone_ids[observed], ROLE_OBSERVES))

    for mapping in (await session.scalars(select(EntityZone))).all():
        key = (mapping.entity_id, mapping.zone_id, mapping.role)
        if key in wanted:
            wanted.discard(key)
        else:
            await session.delete(mapping)

    for entity_id, zone_id, role in wanted:
        session.add(EntityZone(entity_id=entity_id, zone_id=zone_id, role=role))


def _entities_of(camera: CameraConfig) -> list[str]:
    return [e for e in (camera.camera_entity, camera.event_image_entity) if e]


# --------------------------------------------------------------------------- #
# Queries -- deterministic set lookups, no inference
# --------------------------------------------------------------------------- #


async def resolve_zone_id(session: AsyncSession, entity_id: str | None) -> int | None:
    """The zone an entity sits in. Used to place an incoming event."""
    if not entity_id:
        return None
    return await session.scalar(
        select(EntityZone.zone_id).where(
            EntityZone.entity_id == entity_id, EntityZone.role == ROLE_LOCATED_IN
        )
    )


async def zone_by_key(session: AsyncSession, key: str) -> Zone | None:
    return await session.scalar(select(Zone).where(Zone.key == key))


async def all_zones(session: AsyncSession) -> list[Zone]:
    return list((await session.scalars(select(Zone).order_by(Zone.key))).all())


async def adjacent_zone_keys(session: AsyncSession, key: str) -> list[str]:
    """Zone keys directly connected to ``key`` by any relation."""
    zone = await zone_by_key(session, key)
    if zone is None:
        return []
    rows = await session.execute(
        select(Zone.key)
        .join(ZoneEdge, ZoneEdge.to_zone_id == Zone.id)
        .where(ZoneEdge.from_zone_id == zone.id)
    )
    return sorted({row[0] for row in rows})


async def zone_relations(session: AsyncSession, key: str) -> list[dict[str, str]]:
    """Outgoing edges from ``key`` as ``{to, relation}`` pairs."""
    zone = await zone_by_key(session, key)
    if zone is None:
        return []
    rows = await session.execute(
        select(Zone.key, ZoneEdge.relation)
        .join(ZoneEdge, ZoneEdge.to_zone_id == Zone.id)
        .where(ZoneEdge.from_zone_id == zone.id)
        .order_by(Zone.key)
    )
    return [{"to": to_key, "relation": relation} for to_key, relation in rows]


async def entities_observing(session: AsyncSession, zone_key: str) -> list[str]:
    """Entity IDs that can see into ``zone_key``."""
    zone = await zone_by_key(session, zone_key)
    if zone is None:
        return []
    rows = await session.scalars(
        select(EntityZone.entity_id).where(
            EntityZone.zone_id == zone.id, EntityZone.role == ROLE_OBSERVES
        )
    )
    return sorted(set(rows.all()))


def cameras_observing(cameras: CamerasConfig, zone_key: str) -> list[str]:
    """Camera keys that observe ``zone_key``, or are located in it.

    Reads the config rather than the database: the config is the source of truth
    for camera identity, and this keeps a camera *key* (which the database never
    stores) out of the schema.
    """
    return sorted(
        key
        for key, camera in cameras.cameras.items()
        if zone_key in camera.observes or camera.location == zone_key
    )


def coverage_of(cameras: CamerasConfig, zone_key: str) -> str:
    """How well a zone is watched: ``full``, ``partial`` or ``none``.

    Returned alongside query results so an agent answering "did anything happen
    in X" can qualify an empty answer without having to think to ask about
    coverage separately.
    """
    if zone_key not in zones_covered_by(cameras):
        return "none"
    return "partial" if zone_key in zones_partially_covered_by(cameras) else "full"


def zones_partially_covered_by(cameras: CamerasConfig) -> set[str]:
    """Zones some camera sees only part of.

    A zone another camera covers fully is not partial: full coverage wins.
    """
    partial: set[str] = set()
    full: set[str] = set()
    for camera in cameras.cameras.values():
        declared = set(camera.partial_coverage)
        partial |= declared
        full |= ({camera.location, *camera.observes}) - declared
    return partial - full


def zones_covered_by(cameras: CamerasConfig) -> set[str]:
    """Every zone some camera can see. The rest of the house is unobserved."""
    covered: set[str] = set()
    for camera in cameras.cameras.values():
        covered.add(camera.location)
        covered.update(camera.observes)
    return covered
