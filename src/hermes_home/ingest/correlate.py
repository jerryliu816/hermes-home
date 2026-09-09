"""Grouping events into incidents.

One rule, deliberately: an event joins an open incident in the same zone if that
incident was active within the correlation window, otherwise it starts a new one.

No graph traversal, no cross-camera identity matching, no trajectory estimation.
``correlator`` and ``correlator_version`` are recorded on every incident so a
better rule later means deleting and recomputing -- incidents are derived data,
and treating them as disposable is what keeps the door open.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_home.core.ids import new_uid
from hermes_home.core.time import ensure_utc, now_utc
from hermes_home.storage.models import Event, EventTag, Incident, Zone
from hermes_home.storage.repositories import IncidentRepository

logger = structlog.get_logger(__name__)

CORRELATOR_NAME = "same_zone_window_v1"
CORRELATOR_VERSION = 1


async def correlate(
    incidents: IncidentRepository,
    event: Event,
    *,
    window_seconds: int,
    occurred_at: datetime,
) -> Incident | None:
    """Attach ``event`` to an incident, creating one if needed."""
    if event.zone_id is None:
        return None

    incident = await incidents.find_open_in_zone(
        zone_id=event.zone_id, window_seconds=window_seconds, at=occurred_at
    )

    if incident is None:
        moment = now_utc()
        incident = Incident(
            uid=new_uid(),
            incident_type="activity",
            zone_id=event.zone_id,
            started_at=occurred_at,
            ended_at=occurred_at,
            status="open",
            summary=None,
            correlator=CORRELATOR_NAME,
            correlator_version=CORRELATOR_VERSION,
            attributes={},
            created_at=moment,
            updated_at=moment,
        )
        await incidents.create(incident)
    else:
        if incident.ended_at is None or occurred_at > incident.ended_at:
            incident.ended_at = occurred_at
        incident.updated_at = now_utc()

    event.incident_id = incident.id
    return incident


def _format_duration(seconds: float) -> str:
    """Compact, deterministic duration. Stable across runs and machines."""
    total = round(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m" if secs == 0 else f"{minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


def build_incident_summary(
    *, event_count: int, zone_key: str | None, duration_seconds: float, tags: list[str]
) -> str:
    """A factual, deterministic one-line description of an incident.

    Structured rather than narrative on purpose: this is a record, and Hermes
    owns natural-language synthesis. No model is consulted -- the same inputs
    always produce the same string, which is what makes it safe to recompute if
    the correlator ever changes.

        "2 events in front_entry over 35s; person_present, package_present"
    """
    noun = "event" if event_count == 1 else "events"
    where = zone_key or "an unmapped zone"
    summary = f"{event_count} {noun} in {where}"

    # A single instantaneous event has no span worth reporting.
    if duration_seconds > 0:
        summary += f" over {_format_duration(duration_seconds)}"

    if tags:
        summary += "; " + ", ".join(sorted(set(tags)))
    return summary


async def close_stale_incidents(
    session: AsyncSession, *, idle_seconds: int, now: datetime | None = None
) -> int:
    """Close incidents that can no longer receive a correlated event.

    An incident is open while another observation could still join it -- that is,
    within the correlation window of its last event. Past that it is settled, so
    it is closed and given a summary.

    Idempotent: already-closed incidents are excluded by the status filter, so
    running this repeatedly is a no-op. Only ``status``, ``summary`` and
    ``updated_at`` are touched; event membership is never altered.
    """
    moment = ensure_utc(now or now_utc())
    cutoff = moment - timedelta(seconds=idle_seconds)

    stale = (
        await session.scalars(
            select(Incident).where(
                Incident.status == "open",
                func.coalesce(Incident.ended_at, Incident.started_at) < cutoff,
            )
        )
    ).all()

    for incident in stale:
        rows = (
            await session.execute(
                select(Event.id, Event.occurred_at).where(Event.incident_id == incident.id)
            )
        ).all()
        event_ids = [row[0] for row in rows]
        times = [row[1] for row in rows]

        tags: list[str] = []
        if event_ids:
            tags = list(
                (
                    await session.scalars(
                        select(EventTag.tag).where(EventTag.event_id.in_(event_ids)).distinct()
                    )
                ).all()
            )

        zone_key = None
        if incident.zone_id is not None:
            zone = await session.get(Zone, incident.zone_id)
            zone_key = zone.key if zone else None

        # Prefer the events' own span; fall back to the incident's recorded
        # bounds when an incident somehow has no events attached.
        if times:
            duration = (max(times) - min(times)).total_seconds()
        elif incident.ended_at is not None:
            duration = (incident.ended_at - incident.started_at).total_seconds()
        else:
            duration = 0.0

        incident.summary = build_incident_summary(
            event_count=len(event_ids),
            zone_key=zone_key,
            duration_seconds=duration,
            tags=tags,
        )
        incident.status = "closed"
        incident.updated_at = moment

    if stale:
        logger.info("incidents.closed", count=len(stale), idle_seconds=idle_seconds)
    return len(stale)
