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
    CameraHealthView,
    CameraPipelineHealthView,
    CameraView,
    CoverageSpan,
    CoverageView,
    EventView,
    HomeView,
    PipelineCoverageView,
    PipelineGapView,
    ZoneView,
)
from hermes_home.health.coverage import (
    Coverage,
    get_camera_coverage,
    get_pipeline_coverage,
    get_zone_coverage,
    meaning_of,
)
from hermes_home.spatial import (
    adjacent_zone_keys,
    all_zones,
    cameras_observing,
    coverage_of,
    zone_by_key,
    zone_relations,
    zones_covered_by,
    zones_partially_covered_by,
)
from hermes_home.storage.models import (
    CameraHealth,
    Event,
    EventAnalysis,
    EventTag,
    HealthReason,
    HealthStatus,
    Incident,
    PipelineStatus,
    VerificationMode,
    Zone,
)
from hermes_home.storage.repositories import (
    CameraHealthRepository,
    DeliveryGapRepository,
    EventRepository,
)

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
        self._health = CameraHealthRepository(session)
        self._gaps = DeliveryGapRepository(session)

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
        health = {h.camera_key: h for h in await self._health.all_current()}
        return HomeView(
            name=self._settings.home_name,
            timezone=self._settings.display_timezone,
            zones=zones,
            cameras=[
                CameraView(
                    key=key,
                    name=camera.name,
                    aliases=list(camera.aliases),
                    located_in=camera.location,
                    observes=sorted(camera.observes),
                    partial_coverage=sorted(camera.partial_coverage),
                    # Where a camera points and whether it works are different
                    # facts, so they sit in different fields and are never
                    # collapsed into one.
                    current_health=self._effective_status(health.get(key))[0],
                    health_checked_at=(health[key].checked_at if key in health else None),
                )
                for key, camera in sorted(self._cameras.cameras.items())
            ],
            unobserved_zones=sorted(z.key for z in zones if z.key not in covered),
            partially_observed_zones=sorted(zones_partially_covered_by(self._cameras)),
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
    # Camera health and historical coverage

    def _effective_status(self, row: CameraHealth | None) -> tuple[str | None, str | None]:
        """Current status as it should be *reported*, with staleness applied.

        A persisted "healthy" row asserts health for as long as it exists. If
        the monitor died an hour ago, that assertion is a fabricated all-clear
        of exactly the kind this feature exists to eliminate -- one layer up
        from a fabricated coverage interval. So freshness is judged here, at
        read time, which a dead monitor cannot influence by not writing.

        Returns (status, reason); (None, ...) when the camera was never seen.
        """
        if not self._settings.camera_health_enabled:
            return HealthStatus.UNKNOWN, HealthReason.MONITORING_DISABLED
        if row is None:
            return None, HealthReason.TRACKING_NOT_STARTED
        age = (now_utc() - ensure_utc(row.checked_at)).total_seconds()
        if age > self._settings.camera_health_stale_after_seconds:
            return HealthStatus.UNKNOWN, HealthReason.DATA_STALE
        return row.status, row.reason

    async def camera_health(self, camera: str | None = None) -> list[CameraHealthView]:
        """Current health for every configured camera, or one of them."""
        rows = {h.camera_key: h for h in await self._health.all_current()}
        keys = (
            [camera]
            if camera is not None and camera in self._cameras.cameras
            else sorted(self._cameras.cameras)
            if camera is None
            else []
        )

        views: list[CameraHealthView] = []
        for key in keys:
            config = self._cameras.cameras[key]
            row = rows.get(key)
            status, reason = self._effective_status(row)
            views.append(
                CameraHealthView(
                    key=key,
                    name=config.name,
                    aliases=list(config.aliases),
                    located_in=config.location,
                    observes=sorted(config.observes),
                    partial_coverage=sorted(config.partial_coverage),
                    status=status or HealthStatus.UNKNOWN,
                    reason=reason,
                    persisted_status=row.status if row else None,
                    checked_at=row.checked_at if row else None,
                    last_healthy_at=row.last_healthy_at if row else None,
                    offline_since=row.offline_since if row else None,
                    camera_state=row.camera_state if row else None,
                    image_state=row.image_state if row else None,
                    last_image_update_at=row.last_image_update_at if row else None,
                    # Derived from events rather than stored: the events table
                    # is the authority for when an event happened, and a copy
                    # here would go stale the moment health and ingestion
                    # diverge.
                    last_event_at=await self.latest_event_time(camera=key),
                    monitored=self._settings.camera_health_enabled,
                )
            )
        return views

    async def coverage_for(
        self,
        *,
        start: datetime,
        end: datetime,
        camera: str | None = None,
        zone: str | None = None,
    ) -> CoverageView | None:
        """Operational coverage over a period, for one camera or one zone.

        Returns None when neither is named -- coverage of "everywhere" is not a
        question with a determinate answer, and inventing one would be worse
        than declining.
        """
        if camera is not None:
            if camera not in self._cameras.cameras:
                return None
            result = await get_camera_coverage(
                self._health,
                camera,
                start=ensure_utc(start),
                end=ensure_utc(end),
                grace_seconds=self._settings.camera_health_gap_tolerance_seconds,
            )
            view = self._to_coverage_view(result, field_of_view=None)
            return await self._annotate_observed_events(view, camera=camera, zone=None)

        if zone is not None:
            result = await get_zone_coverage(
                self._health,
                self._cameras,
                zone,
                start=ensure_utc(start),
                end=ensure_utc(end),
                grace_seconds=self._settings.camera_health_gap_tolerance_seconds,
            )
            view = self._to_coverage_view(result, field_of_view=self._field_of_view(zone))
            return await self._annotate_observed_events(view, camera=None, zone=zone)

        return None

    async def pipeline_health(self, camera_key: str) -> CameraPipelineHealthView:
        """Current delivery-path health for one camera.

        A quiet camera is never degraded. If reconciliation is running and
        finding nothing wrong, the mechanism is working even though nothing has
        fired to exercise it end to end -- that is ``no_recent_trigger``, not a
        fault. Requiring traffic to claim health would make every quiet night
        look like an outage.
        """
        if not self._settings.delivery_reconciliation_enabled:
            return CameraPipelineHealthView(
                status=PipelineStatus.UNKNOWN,
                verification_mode=VerificationMode.PASSIVE,
                reason="delivery_reconciliation_disabled",
            )

        state = await self._gaps.get_state(camera_key)
        if state is None:
            return CameraPipelineHealthView(
                status=PipelineStatus.UNKNOWN,
                verification_mode=VerificationMode.PASSIVE,
                reason=HealthReason.TRACKING_NOT_STARTED,
            )

        now = now_utc()
        stale_after = self._settings.delivery_reconciliation_interval_seconds * 3
        if (now - ensure_utc(state.last_checked_at)).total_seconds() > stale_after:
            return CameraPipelineHealthView(
                status=PipelineStatus.UNKNOWN,
                verification_mode=VerificationMode.PASSIVE,
                reason=HealthReason.DATA_STALE,
                last_verified_delivery_at=state.last_verified_delivery_at,
                last_reconciliation_check_at=state.last_checked_at,
            )

        lookback = timedelta(seconds=self._settings.delivery_reconciliation_lookback_seconds)
        open_gaps = await self._gaps.gaps_between(
            start=now - lookback, end=now, camera_key=camera_key
        )
        verified = state.last_verified_delivery_at
        recently_verified = verified is not None and (now - ensure_utc(verified)) <= lookback

        return CameraPipelineHealthView(
            status=PipelineStatus.DEGRADED if open_gaps else PipelineStatus.HEALTHY,
            verification_mode=(
                VerificationMode.ACTIVE if recently_verified else VerificationMode.NO_RECENT_TRIGGER
            ),
            reason=open_gaps[0].reason if open_gaps else None,
            last_verified_delivery_at=verified,
            last_reconciliation_check_at=state.last_checked_at,
            open_gap_count=len(open_gaps),
        )

    async def pipeline_coverage_for(
        self,
        *,
        start: datetime,
        end: datetime,
        camera: str | None = None,
        zone: str | None = None,
    ) -> PipelineCoverageView | None:
        """Delivery coverage for one camera, or every camera watching one zone."""
        if camera is not None:
            keys = [camera] if camera in self._cameras.cameras else []
        elif zone is not None:
            keys = cameras_observing(self._cameras, zone)
        else:
            return None
        if not keys:
            return None

        result = await get_pipeline_coverage(
            self._gaps,
            self._cameras,
            start=ensure_utc(start),
            end=ensure_utc(end),
            camera_keys=keys,
        )
        return PipelineCoverageView(
            start=result.start,
            end=result.end,
            complete=result.complete,
            reason=result.reason,
            meaning=meaning_of(result.reason),
            cameras_considered=result.cameras_considered,
            delivery_gaps=[PipelineGapView(**g.as_dict()) for g in result.gaps],
            unknown_periods=[CoverageSpan(**u.as_dict_typed()) for u in result.unknown_periods],
        )

    def _field_of_view(self, zone: str) -> dict[str, object]:
        """Static coverage of a zone: which cameras point at it, and how fully."""
        return {
            "zone": zone,
            "status": coverage_of(self._cameras, zone),
            "cameras": cameras_observing(self._cameras, zone),
        }

    async def _annotate_observed_events(
        self, view: CoverageView, *, camera: str | None, zone: str | None
    ) -> CoverageView:
        """Count events actually recorded inside each unverified span.

        An event inside a monitoring gap is positive proof that the camera and
        the pipeline both worked at that instant -- so "no activity was
        recorded" is simply false about that period, and Hermes needs to be able
        to see that without inferring it.

        It deliberately does not shorten, split or reclassify the span. One
        event at 07:34 says nothing about 07:35, and letting evidence of a
        single moment stand in for continuous coverage is exactly the
        over-claim this whole feature exists to prevent.
        """
        for span in view.unknown_periods:
            events = await self.search_events(
                start=span.start, end=span.end, camera=camera, zone=zone, limit=MAX_LIMIT
            )
            span.events_observed = len(events)
        return view

    @staticmethod
    def _to_coverage_view(
        result: Coverage, *, field_of_view: dict[str, object] | None
    ) -> CoverageView:
        return CoverageView(
            start=result.start,
            end=result.end,
            complete=result.complete,
            reason=result.reason,
            cameras_considered=result.cameras_considered,
            coverage_gaps=[CoverageSpan(**g.as_dict_typed()) for g in result.gaps],
            unknown_periods=[CoverageSpan(**u.as_dict_typed()) for u in result.unknown_periods],
            field_of_view=field_of_view,
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
