"""The MCP tool surface Hermes talks to.

Scope is deliberate. Hermes already owns Home Assistant's *current* state through
its own `ha_get_state` / `ha_list_entities` / `ha_call_service` tools, so nothing
here duplicates them: no entity passthrough, no service calls, no raw SQL, no
generic database access. What Hermes cannot get anywhere else is persistent
semantic history, so that is exactly and only what this exposes.

Everything here is deterministic. No tool calls a language model. Hermes does the
reasoning and the prose; this hands it structured facts to reason over.

Tool names are an external contract -- they end up in Hermes' config and prompts,
so renaming one later means touching another system. They are prefixed `home_`
and are expected to be stable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

import structlog
from pydantic import Field

from hermes_home.api.deps import AppState
from hermes_home.core.time import ensure_utc, now_utc, parse_ha_timestamp
from hermes_home.services.event_service import MAX_LIMIT, EventService
from hermes_home.spatial import cameras_observing, coverage_of
from hermes_home.storage.engine import session_scope

logger = structlog.get_logger(__name__)


def _parse_time(value: str | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_ha_timestamp(value)
    except ValueError as exc:
        raise ValueError(
            f"{field} must be ISO 8601 with a UTC offset, e.g. 2026-09-08T15:00:00Z"
        ) from exc


async def _coverage_blocks(
    svc: EventService,
    *,
    start: datetime | None,
    end: datetime | None,
    camera: str | None,
    zone: str | None,
) -> dict[str, Any]:
    """The two independent operational dimensions for a bounded, placed query.

    Attached automatically rather than left to a separate call: the caller must
    not be able to read ``events: []`` as "nothing happened" without also seeing
    whether anything was watching and whether what was seen reached us. Making
    either a separate tool Hermes has to remember to call would guarantee it is
    sometimes skipped -- and the one time it is skipped is the time it mattered.

    They are reported separately because they fail separately. On 2026-09-09
    every camera was healthy and four deliveries were lost in transit; a single
    merged "coverage" number would have had to pick one of those to report.
    """
    if start is None or end is None or (camera is None and zone is None):
        return {}
    blocks: dict[str, Any] = {}
    health = await svc.coverage_for(start=start, end=end, camera=camera, zone=zone)
    if health:
        blocks["camera_health_coverage"] = health.model_dump(mode="json")
    pipeline = await svc.pipeline_coverage_for(start=start, end=end, camera=camera, zone=zone)
    if pipeline:
        blocks["event_pipeline_coverage"] = pipeline.model_dump(mode="json")
    return blocks


def _coverage_note(state: AppState, zone: str | None) -> dict[str, Any] | None:
    """Describe how well a zone is watched, for attaching to a query result."""
    if not zone:
        return None
    status = coverage_of(state.cameras, zone)
    watching = cameras_observing(state.cameras, zone)
    named = ", ".join(watching)

    if status == "none":
        note = (
            f"No camera watches {zone}. An absence of events says nothing about what "
            "happened there."
        )
    elif status == "partial":
        several = len(watching) > 1
        note = (
            f"{'Cameras' if several else 'Camera'} {named} "
            f"{'cover' if several else 'covers'} only part of {zone}; some of it is in "
            "no camera's view. An absence of events is weaker evidence here than in a "
            "fully covered zone."
        )
    else:
        note = f"{zone} is fully covered by {named}."

    return {"zone": zone, "status": status, "cameras": watching, "note": note}


def register_tools(mcp: Any, state: AppState) -> None:
    """Attach every tool to ``mcp``, closing over the shared application state."""

    def service(session: Any) -> EventService:
        return EventService(session, settings=state.settings, cameras=state.cameras)

    # ----------------------------------------------------------------- #

    @mcp.tool(
        name="home_recent_events",
        title="Recent home events",
        description=(
            "THE DEFAULT TOOL for any question about what has happened around the home. "
            "Returns stored camera and sensor observations, newest first, with what was "
            "seen in each.\n\n"
            "Use it for questions like: 'What happened at the front door?', 'What "
            "happened recently?', 'Who or what was detected?', 'Did anyone come to the "
            "house?', 'Was a package delivered?', 'What happened while I was away?', "
            "'What camera activity occurred?'\n\n"
            "Call this FIRST for such questions - do not inspect Home Assistant, "
            "containers, the filesystem, or the desktop to answer them. This is stored "
            "history; for the CURRENT state of a device, or to control one, use your "
            "Home Assistant tools instead.\n\n"
            "Call it with NO arguments (or camera/zone only) unless the user named a "
            "time period. The default window is 24 hours; narrowing it to an hour makes "
            "an ordinary 'what happened' question return nothing.\n\n"
            "If the result is empty it reports latest_event_at and a hint naming the "
            "exact `minutes` value that reaches the most recent event - follow it rather "
            "than concluding there is no data or looking somewhere else.\n\n"
            "When camera or zone is given, a `coverage` block says whether those cameras "
            "were actually WORKING during the window. Never report an empty result as "
            "'nothing happened' without checking it: coverage.complete false means a "
            "camera was down, and null means nobody was recording health then. Both are "
            "different from 'all quiet'."
        ),
    )
    async def home_recent_events(
        minutes: Annotated[
            int,
            Field(
                description=(
                    "How far back to look, in minutes. Omit this unless the user named "
                    "a period; the 1440-minute (24 hour) default is the right choice "
                    "for an open-ended question. Increase it to reach older events."
                ),
                ge=1,
                le=525_600,
            ),
        ] = 1440,
        camera: Annotated[str | None, Field(description="Camera key, e.g. 'front_door'.")] = None,
        zone: Annotated[str | None, Field(description="Zone key, e.g. 'front_entry'.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 20,
    ) -> dict[str, Any]:
        async with session_scope(state.session_factory) as session:
            svc = service(session)
            window_end = now_utc()
            window_start = window_end - timedelta(minutes=minutes)
            events = await svc.recent_events(minutes=minutes, camera=camera, zone=zone, limit=limit)
            result: dict[str, Any] = {
                "window_minutes": minutes,
                "count": len(events),
                "events": [e.model_dump(mode="json") for e in events],
            }
            coverage = _coverage_note(state, zone)
            if coverage:
                result["field_of_view"] = coverage
            result.update(
                await _coverage_blocks(
                    svc, start=window_start, end=window_end, camera=camera, zone=zone
                )
            )
            if not events:
                # An empty window is the moment a caller is most likely to give
                # up on this tool and go looking elsewhere. Say what does exist.
                latest = await svc.latest_event_time(camera=camera, zone=zone)
                result["latest_event_at"] = latest.isoformat() if latest else None
                if latest is not None:
                    age_minutes = int((now_utc() - latest).total_seconds() // 60)
                    result["hint"] = (
                        f"No events in the last {minutes} minutes, but the most recent "
                        f"matching event was {latest.isoformat()} "
                        f"({age_minutes} minutes ago). Call this tool again with "
                        f"minutes={age_minutes + 60} to include it."
                    )
                else:
                    result["hint"] = (
                        "No matching events are stored at all. Check "
                        "home_describe_home for unobserved_zones: a zone no camera "
                        "watches records nothing, which is not the same as nothing "
                        "having happened."
                    )
        # zone included: without it a zone-filtered empty result is
        # indistinguishable from a broken query when reading the trail.
        logger.info(
            "mcp.home_recent_events",
            minutes=minutes,
            returned=len(events),
            camera=camera,
            zone=zone,
        )
        return result

    @mcp.tool(
        name="home_search_events",
        title="Search home events",
        description=(
            "Search stored home observations by time range, camera, zone, event type or "
            "tag. Use when the question names a specific period, place or thing: "
            "'What happened yesterday afternoon?', 'Was there anyone at the driveway on "
            "Tuesday?', 'When was the last package delivery?', 'Show me every person "
            "detection this week.'\n\n"
            "For an open-ended 'what happened' with no time bound, prefer "
            "home_recent_events. This is stored history, not live device state.\n\n"
            "Times are ISO 8601 and must carry a UTC offset (2026-09-08T15:00:00Z). "
            "event_type matches by prefix, so 'camera.' matches every camera event. "
            "Tags are ANDed; available tags include person_present, package_present, "
            "vehicle_present, animal_present.\n\n"
            "When the search is time-bounded AND filtered by camera or zone, a `coverage` "
            "block reports whether those cameras were working across that period. An "
            "empty event list with coverage.complete false or null does NOT mean nothing "
            "happened - say what was actually unobserved."
        ),
    )
    async def home_search_events(
        start_time: Annotated[
            str | None, Field(description="Inclusive start, ISO 8601, e.g. 2026-09-08T00:00:00Z.")
        ] = None,
        end_time: Annotated[str | None, Field(description="Inclusive end, ISO 8601.")] = None,
        camera: Annotated[str | None, Field(description="Camera key.")] = None,
        zone: Annotated[str | None, Field(description="Zone key.")] = None,
        event_type: Annotated[
            str | None, Field(description="Prefix match, e.g. 'camera.person_detected'.")
        ] = None,
        tags: Annotated[
            list[str] | None, Field(description="All listed tags must be present.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 50,
    ) -> dict[str, Any]:
        start = _parse_time(start_time, field="start_time")
        end = _parse_time(end_time, field="end_time")
        if start and end and start > end:
            raise ValueError("start_time must not be after end_time")

        async with session_scope(state.session_factory) as session:
            svc = service(session)
            events = await svc.search_events(
                start=start,
                end=end,
                camera=camera,
                zone=zone,
                event_type=event_type,
                tags=tags,
                limit=limit,
            )
            operational = await _coverage_blocks(
                svc, start=start, end=end, camera=camera, zone=zone
            )
        logger.info(
            "mcp.home_search_events",
            returned=len(events),
            camera=camera,
            zone=zone,
            event_type=event_type,
        )
        result: dict[str, Any] = {
            "count": len(events),
            "events": [e.model_dump(mode="json") for e in events],
        }
        coverage = _coverage_note(state, zone)
        if coverage:
            result["field_of_view"] = coverage
        result.update(operational)
        return result

    @mcp.tool(
        name="home_get_event",
        title="Get one home event",
        description=(
            "Full detail for one event by its uid, including every field of the "
            "structured observation. Use after home_recent_events or home_search_events "
            "when you need more than the summary of a specific event."
        ),
    )
    async def home_get_event(
        uid: Annotated[str, Field(description="Event uid from a search result.")],
    ) -> dict[str, Any]:
        async with session_scope(state.session_factory) as session:
            event = await service(session).get_event(uid)
        if event is None:
            return {"found": False, "uid": uid, "event": None}
        return {"found": True, "uid": uid, "event": event.model_dump(mode="json")}

    @mcp.tool(
        name="home_list_zones",
        title="List home zones",
        description=(
            "Every zone of the property, how zones connect, and which cameras observe "
            "each. Use to translate a place the user named ('the driveway', 'out back') "
            "into a zone key for the other tools, or to check whether anything watches "
            "an area before reporting that nothing happened there."
        ),
    )
    async def home_list_zones() -> dict[str, Any]:
        async with session_scope(state.session_factory) as session:
            zones = await service(session).list_zones()
        return {"count": len(zones), "zones": [z.model_dump(mode="json") for z in zones]}

    @mcp.tool(
        name="home_describe_home",
        title="Describe the home",
        description=(
            "The layout of the property: zones, their relationships, the cameras and what "
            "each observes, and which zones nothing watches. Use for 'what can you see?', "
            "'which cameras are there?', 'is the backyard covered?'\n\n"
            "Each camera reports current_health alongside what it observes. These are "
            "different facts: observes/partial_coverage say where a camera POINTS, "
            "current_health says whether it is WORKING. A camera can be configured to "
            "watch the backyard and be offline right now.\n\n"
            "Two fields qualify what silence means. unobserved_zones: no camera "
            "watches it at all, so an absence of events says nothing about what "
            "happened. partially_observed_zones: a camera sees only part of it, so an "
            "absence is weaker evidence than in a fully covered zone. In both cases "
            "say what is actually known instead of reporting an all-clear."
        ),
    )
    async def home_describe_home() -> dict[str, Any]:
        async with session_scope(state.session_factory) as session:
            home = await service(session).describe_home()
        return home.model_dump(mode="json")

    @mcp.tool(
        name="home_list_cameras",
        title="List cameras and their health",
        description=(
            "Every camera, what it watches, and whether it is CURRENTLY WORKING. Use for "
            "'are all my cameras working?', 'which cameras are offline?', 'is the "
            "backyard camera up?', 'when did the driveway camera last see anything?'\n\n"
            "status is healthy | degraded | offline | unknown. unknown genuinely means "
            "not determinable - Home Assistant unreachable, monitoring disabled, or the "
            "health data gone stale (see `reason`) - and must never be reported as "
            "working. degraded means reachable but its event-image entity is "
            "unavailable, so it would not produce an analyzable frame.\n\n"
            "last_event_at is NOT a health signal. A camera with no events for days may "
            "be perfectly healthy in a quiet week; do not infer a fault from silence.\n\n"
            "This is current state. For whether a camera was working during some past "
            "period, use home_coverage."
        ),
    )
    async def home_list_cameras(
        camera: Annotated[
            str | None, Field(description="One camera key; omit for all of them.")
        ] = None,
    ) -> dict[str, Any]:
        async with session_scope(state.session_factory) as session:
            svc = service(session)
            views = await svc.camera_health(camera)
            pipeline = {v.key: await svc.pipeline_health(v.key) for v in views}
        if camera is not None and not views:
            return {"count": 0, "cameras": [], "error": f"no camera configured as {camera!r}"}
        by_status: dict[str, int] = {}
        for view in views:
            by_status[view.status] = by_status.get(view.status, 0) + 1
        logger.info("mcp.home_list_cameras", returned=len(views), camera=camera)
        cameras: list[dict[str, Any]] = []
        for v in views:
            row = v.model_dump(mode="json")
            row["event_pipeline_health"] = pipeline[v.key].model_dump(mode="json")
            cameras.append(row)
        return {"count": len(views), "by_status": by_status, "cameras": cameras}

    @mcp.tool(
        name="home_coverage",
        title="Historical camera coverage",
        description=(
            "Whether a camera or zone was actually being WATCHED during a past period. "
            "Use to qualify any negative answer about the past: 'did I have coverage of "
            "the backyard last night?', 'was the driveway camera up between 2 and 6 AM?', "
            "'can I trust that nothing happened out back?'\n\n"
            "complete is three-valued and the difference matters:\n"
            "  true  - confirmed watched throughout\n"
            "  false - a known gap; see coverage_gaps\n"
            "  null  - CANNOT BE DETERMINED, not 'fine'. Health was not being recorded "
            "then: before this feature was deployed, while the service was down, or "
            "while Home Assistant was unreachable. Never report null as an all-clear.\n\n"
            "For a zone, complete true means AT LEAST ONE camera covering that zone was "
            "working throughout - not that all of them were. field_of_view is a separate "
            "fact about where cameras point, and may still be partial or none even when "
            "coverage is complete: the equipment worked, but it never saw all of the area."
        ),
    )
    async def home_coverage(
        start_time: Annotated[str, Field(description="Inclusive start, ISO 8601 with offset.")],
        end_time: Annotated[str, Field(description="Inclusive end, ISO 8601 with offset.")],
        camera: Annotated[str | None, Field(description="Camera key. One of camera/zone.")] = None,
        zone: Annotated[str | None, Field(description="Zone key. One of camera/zone.")] = None,
    ) -> dict[str, Any]:
        start = _parse_time(start_time, field="start_time")
        end = _parse_time(end_time, field="end_time")
        if start is None or end is None:
            raise ValueError("both start_time and end_time are required")
        if start > end:
            raise ValueError("start_time must not be after end_time")
        if (camera is None) == (zone is None):
            # Returned rather than raised: the server hides exception text, and
            # a caller that cannot see what it got wrong cannot correct it.
            return {
                "error": "give exactly one of camera or zone",
                "coverage": None,
            }

        async with session_scope(state.session_factory) as session:
            svc = service(session)
            view = await svc.coverage_for(start=start, end=end, camera=camera, zone=zone)
            pipeline = await svc.pipeline_coverage_for(
                start=start, end=end, camera=camera, zone=zone
            )
        if view is None:
            return {
                "error": f"no camera configured as {camera!r}",
                "camera_health_coverage": None,
            }
        logger.info(
            "mcp.home_coverage",
            camera=camera,
            zone=zone,
            health_complete=view.complete,
            pipeline_complete=pipeline.complete if pipeline else None,
        )
        result: dict[str, Any] = {"camera_health_coverage": view.model_dump(mode="json")}
        if pipeline:
            result["event_pipeline_coverage"] = pipeline.model_dump(mode="json")
        return result

    @mcp.tool(
        name="home_summarize_activity",
        title="Summarize home activity",
        description=(
            "Aggregate counts for a period, plus the events in chronological order. Use "
            "for questions about a whole span rather than a single event: 'Summarize "
            "today's activity', 'What happened around the house today?', 'How many "
            "events happened this morning?', 'How busy was the front door this week?'\n\n"
            "Defaults to the last 24 hours. Returns structured counts only, never prose "
            "- write the narrative yourself from these numbers.\n\n"
            "With a zone or camera, a `coverage` block reports whether those cameras were "
            "working across the period. A count of zero with incomplete coverage is not a "
            "quiet period; disclose the gap."
        ),
    )
    async def home_summarize_activity(
        start_time: Annotated[
            str | None, Field(description="ISO 8601. Defaults to 24 hours before end_time.")
        ] = None,
        end_time: Annotated[str | None, Field(description="ISO 8601. Defaults to now.")] = None,
        zone: Annotated[str | None, Field(description="Restrict to one zone key.")] = None,
        camera: Annotated[str | None, Field(description="Restrict to one camera key.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 100,
    ) -> dict[str, Any]:
        end = _parse_time(end_time, field="end_time") or now_utc()
        start = _parse_time(start_time, field="start_time") or (end - timedelta(hours=24))
        if start > end:
            raise ValueError("start_time must not be after end_time")

        async with session_scope(state.session_factory) as session:
            svc = service(session)
            summary = await svc.summarize_activity(
                start=ensure_utc(start), end=ensure_utc(end), zone=zone, limit=limit
            )
            operational = await _coverage_blocks(
                svc, start=ensure_utc(start), end=ensure_utc(end), camera=camera, zone=zone
            )
        logger.info("mcp.home_summarize_activity", event_count=summary.event_count, zone=zone)
        result = summary.model_dump(mode="json")
        note = _coverage_note(state, zone)
        if note:
            result["field_of_view"] = note
        result.update(operational)
        return result
