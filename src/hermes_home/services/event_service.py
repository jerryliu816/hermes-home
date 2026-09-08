"""Read-side services.

Every read goes through here rather than through a transport. The MCP tools and
the HTTP API are both thin callers; if either queried storage directly they would
drift, and the MCP contract is the one Hermes depends on.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_home.config import CamerasConfig, Settings
from hermes_home.core.time import ensure_utc, now_utc, to_display_tz
from hermes_home.domain.dto import (
    ActivitySummary,
    AnalysisView,
    CameraView,
    EventView,
    HomeView,
    ZoneView,
)
from hermes_home.spatial import (
    adjacent_zone_keys,
    all_zones,
    cameras_observing,
    zone_by_key,
    zone_relations,
    zones_covered_by,
)
from hermes_home.storage.models import Event, EventAnalysis, EventTag, Incident, Zone
from hermes_home.storage.repositories import EventRepository

#: A hard ceiling on any single response. Tool results are fed into a model's
#: context, so an unbounded query is a way to blow that up by accident.
MAX_LIMIT = 200


class EventService:
    def __init__(
        self, session: AsyncSession, *, settings: Settings, cameras: CamerasConfig
    ) -> None:
        self._session = session
        self._settings = settings
        self._cameras = cameras
        self._repo = EventRepository(session)

    # ----------------------------------------------------------------- #

    async def recent_events(self, *, minutes: int = 1440, **filters) -> list[EventView]:
        end = now_utc()
        return await self.search_events(start=end - timedelta(minutes=minutes), end=end, **filters)

    async def latest_event_time(
        self, *, camera: str | None = None, zone: str | None = None
    ) -> datetime | None:
        """When the most recent matching event happened, ignoring any window.

        Used to turn an empty result into a signpost. A tool that answers a
        time-bounded question with a bare "0 events" reads as "I have nothing
        for you", which pushes a caller into looking somewhere else entirely.
        Saying "nothing in that window, but there is one from 8:47" keeps the
        conversation on the data that exists.
        """
        stmt = select(func.max(Event.occurred_at))
        zone_id = await self._zone_id(zone)
        if zone is not None and zone_id is None:
            return None
        if zone_id is not None:
            stmt = stmt.where(Event.zone_id == zone_id)
        if camera is not None:
            entities = self._camera_entities(camera)
            if not entities:
                return None
            stmt = stmt.where(Event.source_entity_id.in_(entities))
        return await self._session.scalar(stmt)

    async def search_events(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        camera: str | None = None,
        zone: str | None = None,
        event_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[EventView]:
        zone_id = await self._zone_id(zone)
        if zone is not None and zone_id is None:
            return []

        source_entity_id = None
        if camera is not None:
            entities = self._camera_entities(camera)
            if not entities:
                return []
            # Events are recorded against the entity that triggered them, which
            # for a camera is its event-image entity.
            source_entity_id = entities[0]

        events = await self._repo.search(
            start=start,
            end=end,
            event_type_prefix=event_type,
            zone_id=zone_id,
            source_entity_id=source_entity_id,
            tags=tags,
            limit=max(1, min(limit, MAX_LIMIT)),
        )
        return [await self._to_view(e) for e in events]

    async def get_event(self, uid: str) -> EventView | None:
        event = await self._repo.get_by_uid(uid)
        return await self._to_view(event) if event else None

    # ----------------------------------------------------------------- #

    async def list_zones(self) -> list[ZoneView]:
        views: list[ZoneView] = []
        for zone in await all_zones(self._session):
            views.append(
                ZoneView(
                    key=zone.key,
                    name=zone.name,
                    kind=zone.kind,
                    adjacent_to=await adjacent_zone_keys(self._session, zone.key),
                    relations=await zone_relations(self._session, zone.key),
                    observed_by_cameras=cameras_observing(self._cameras, zone.key),
                )
            )
        return views

    async def describe_home(self) -> HomeView:
        zones = await self.list_zones()
        covered = zones_covered_by(self._cameras)
        return HomeView(
            name=self._settings.home_name,
            timezone=self._settings.display_timezone,
            zones=zones,
            cameras=[
                CameraView(
                    key=key,
                    name=camera.name,
                    located_in=camera.location,
                    observes=sorted(camera.observes),
                )
                for key, camera in sorted(self._cameras.cameras.items())
            ],
            unobserved_zones=sorted(z.key for z in zones if z.key not in covered),
        )

    # ----------------------------------------------------------------- #

    async def summarize_activity(
        self, *, start: datetime, end: datetime, zone: str | None = None, limit: int = 100
    ) -> ActivitySummary:
        """Deterministic counts plus the timeline. No model call, ever."""
        events = await self.search_events(start=start, end=end, zone=zone, limit=limit)

        incident_count = await self._session.scalar(
            select(func.count(func.distinct(Event.incident_id))).where(
                Event.occurred_at >= ensure_utc(start),
                Event.occurred_at <= ensure_utc(end),
                Event.incident_id.is_not(None),
            )
        )

        note = None
        if len(events) >= limit:
            note = f"truncated at {limit} events; narrow the time range for a complete tally"

        return ActivitySummary(
            start=ensure_utc(start),
            end=ensure_utc(end),
            timezone=self._settings.display_timezone,
            event_count=len(events),
            by_type=dict(Counter(e.event_type for e in events)),
            by_zone=dict(Counter(e.zone for e in events if e.zone)),
            by_camera=dict(Counter(e.camera for e in events if e.camera)),
            by_tag=dict(Counter(tag for e in events for tag in e.tags)),
            incident_count=incident_count or 0,
            timeline=list(reversed(events)),  # chronological reads better as a story
            note=note,
        )

    # ----------------------------------------------------------------- #

    async def _to_view(self, event: Event) -> EventView:
        zone_key = zone_name = None
        if event.zone_id is not None:
            zone = await self._session.get(Zone, event.zone_id)
            if zone is not None:
                zone_key, zone_name = zone.key, zone.name

        analysis = await self._session.scalar(
            select(EventAnalysis)
            .where(EventAnalysis.event_id == event.id)
            .order_by(EventAnalysis.attempt.desc())
            .limit(1)
        )
        tags = sorted(
            (
                await self._session.scalars(
                    select(EventTag.tag).where(EventTag.event_id == event.id)
                )
            ).all()
        )

        incident_uid = None
        if event.incident_id is not None:
            incident = await self._session.get(Incident, event.incident_id)
            incident_uid = incident.uid if incident else None

        analysis_view = None
        summary = None
        if analysis is not None:
            analysis_view = AnalysisView(
                status=analysis.status,
                provider=analysis.provider,
                model=analysis.model,
                prompt_version=analysis.prompt_version,
                attempt=analysis.attempt,
                observation=analysis.observation,
                error_code=analysis.error_code,
                latency_ms=analysis.latency_ms,
            )
            if analysis.observation:
                summary = analysis.observation.get("scene_summary")

        return EventView(
            uid=event.uid,
            event_type=event.event_type,
            source=event.source,
            source_entity_id=event.source_entity_id,
            camera=self._camera_key_for(event.source_entity_id),
            zone=zone_key,
            zone_name=zone_name,
            occurred_at=event.occurred_at,
            occurred_at_local=to_display_tz(
                event.occurred_at, self._settings.display_timezone
            ).isoformat(),
            received_at=event.received_at,
            tags=tags,
            summary=summary,
            analysis=analysis_view,
            duplicate_count=event.duplicate_count,
            incident_uid=incident_uid,
        )

    async def _zone_id(self, zone_key: str | None) -> int | None:
        if not zone_key:
            return None
        zone = await zone_by_key(self._session, zone_key)
        return zone.id if zone else None

    def _camera_entities(self, camera_key: str) -> list[str]:
        camera = self._cameras.cameras.get(camera_key)
        if camera is None:
            return []
        return [e for e in (camera.event_image_entity, camera.camera_entity) if e]

    def _camera_key_for(self, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        for key, camera in self._cameras.cameras.items():
            if entity_id in (camera.event_image_entity, camera.camera_entity):
                return key
        return None
