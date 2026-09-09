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
from hermes_home.spatial import coverage_of
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


def _coverage_note(state: AppState, zone: str | None) -> dict[str, Any] | None:
    """Describe how well a zone is watched, for attaching to a query result."""
    if not zone:
        return None
    status = coverage_of(state.cameras, zone)
    note = {
        "none": (
            f"No camera watches {zone}. An absence of events says nothing about what "
            "happened there."
        ),
        "partial": (
            f"Only part of {zone} is in view of a camera. An absence of events is "
            "weaker evidence here than in a fully covered zone."
        ),
        "full": f"{zone} is fully covered by at least one camera.",
    }[status]
    return {"zone": zone, "status": status, "note": note}


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
            "than concluding there is no data or looking somewhere else."
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
            events = await svc.recent_events(minutes=minutes, camera=camera, zone=zone, limit=limit)
            result: dict[str, Any] = {
                "window_minutes": minutes,
                "count": len(events),
                "events": [e.model_dump(mode="json") for e in events],
            }
            coverage = _coverage_note(state, zone)
            if coverage:
                result["zone_coverage"] = coverage
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
            "vehicle_present, animal_present."
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
            events = await service(session).search_events(
                start=start,
                end=end,
                camera=camera,
                zone=zone,
                event_type=event_type,
                tags=tags,
                limit=limit,
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
            result["zone_coverage"] = coverage
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
        name="home_summarize_activity",
        title="Summarize home activity",
        description=(
            "Aggregate counts for a period, plus the events in chronological order. Use "
            "for questions about a whole span rather than a single event: 'Summarize "
            "today's activity', 'What happened around the house today?', 'How many "
            "events happened this morning?', 'How busy was the front door this week?'\n\n"
            "Defaults to the last 24 hours. Returns structured counts only, never prose "
            "- write the narrative yourself from these numbers."
        ),
    )
    async def home_summarize_activity(
        start_time: Annotated[
            str | None, Field(description="ISO 8601. Defaults to 24 hours before end_time.")
        ] = None,
        end_time: Annotated[str | None, Field(description="ISO 8601. Defaults to now.")] = None,
        zone: Annotated[str | None, Field(description="Restrict to one zone key.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 100,
    ) -> dict[str, Any]:
        end = _parse_time(end_time, field="end_time") or now_utc()
        start = _parse_time(start_time, field="start_time") or (end - timedelta(hours=24))
        if start > end:
            raise ValueError("start_time must not be after end_time")

        async with session_scope(state.session_factory) as session:
            summary = await service(session).summarize_activity(
                start=ensure_utc(start), end=ensure_utc(end), zone=zone, limit=limit
            )
        logger.info("mcp.home_summarize_activity", event_count=summary.event_count)
        return summary.model_dump(mode="json")
