"""Grouping events into incidents.

One rule, deliberately: an event joins an open incident in the same zone if that
incident was active within the correlation window, otherwise it starts a new one.

No graph traversal, no cross-camera identity matching, no trajectory estimation.
``correlator`` and ``correlator_version`` are recorded on every incident so a
better rule later means deleting and recomputing -- incidents are derived data,
and treating them as disposable is what keeps the door open.
"""

from __future__ import annotations

from datetime import datetime

from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc
from hermes_home.storage.models import Event, Incident
from hermes_home.storage.repositories import IncidentRepository

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
