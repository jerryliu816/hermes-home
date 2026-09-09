"""Historical coverage: was a camera, or a zone, actually watched then?

Deterministic segment algebra over recorded health intervals. No Home Assistant
calls, no language model, no inference from the presence or absence of events --
coverage is a function of camera health and a time range, and nothing else.
Deriving it from "were there events?" would be circular: the whole question is
whether an empty result means quiet or means blind.

``complete`` is tri-state and the distinction is the point:

    True   coverage confirmed
    False  a known gap exists
    None   cannot be determined

Absence of an outage row is never evidence of health. A period before monitoring
began, or during which hermes-home was not running, is ``None`` -- never ``True``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from hermes_home.config import CamerasConfig
from hermes_home.core.time import ensure_utc
from hermes_home.spatial import cameras_observing
from hermes_home.storage.models import DeliveryGapReason, HealthReason, HealthStatus
from hermes_home.storage.repositories import CameraHealthRepository, DeliveryGapRepository

#: What each reason actually means, in words Hermes can use directly. The
#: structured code is always preserved alongside; this exists so that a gap in
#: OUR OBSERVATION is never rendered as a camera fault. They are different
#: claims about different equipment, and conflating them is how a working
#: camera gets reported as a dead one.
REASON_MEANINGS: dict[str, str] = {
    HealthReason.MONITORING_GAP: (
        "Camera health was not observed during this interval. The camera may have "
        "continued operating normally."
    ),
    HealthReason.BEFORE_TRACKING: (
        "Camera health was not observed during this interval because health tracking "
        "had not started yet. The camera may have continued operating normally."
    ),
    HealthReason.TRACKING_NOT_STARTED: (
        "Camera health has never been observed for this camera. Nothing is known "
        "about whether it was working."
    ),
    HealthReason.HA_UNREACHABLE: (
        "hermes-home could not reach Home Assistant, so camera health could not be "
        "observed. This says nothing about the camera itself."
    ),
    HealthReason.CAMERA_ENTITY_UNAVAILABLE: (
        "Home Assistant reported the camera entity as unavailable: a known device outage."
    ),
    HealthReason.CAMERA_ENTITY_NOT_FOUND: (
        "The configured camera entity does not exist in Home Assistant."
    ),
    HealthReason.EVENT_IMAGE_ENTITY_UNAVAILABLE: (
        "The camera was reachable but its event-image entity was unavailable, so an "
        "event would not have produced an analyzable frame."
    ),
    HealthReason.HEALTH_ENTITY_UNHEALTHY_STATE: (
        "The camera's configured connectivity entity reported a disconnected state."
    ),
    HealthReason.HEALTH_ENTITY_STATE_UNKNOWN: (
        "The camera's connectivity entity reported an unknown state, so health could "
        "not be determined."
    ),
    HealthReason.NO_HEALTH_ENTITY: (
        "No entity is configured for this camera, so its health cannot be observed."
    ),
    HealthReason.NOT_APPLICABLE_NO_CAMERAS: (
        "No camera is configured to watch this zone, so there is no camera health to "
        "report. This is a configuration fact, not an outage."
    ),
    DeliveryGapReason.WEBHOOK_NOT_RECEIVED: (
        "Home Assistant recorded a camera trigger but hermes-home never received the "
        "corresponding delivery, so this event is missing from the stored history."
    ),
}


def meaning_of(reason: str | None) -> str | None:
    """Plain-language meaning for a structured reason code, if we have one."""
    return REASON_MEANINGS.get(reason) if reason else None


#: Statuses that mean the camera was not usefully watching. ``degraded`` counts:
#: it is the state in which the event-image entity is unavailable, so a real
#: event would produce no analyzable frame. Calling that "covered" would be the
#: over-claim this module exists to prevent.
_GAP_STATUSES = frozenset({HealthStatus.OFFLINE, HealthStatus.DEGRADED})


@dataclass
class Segment:
    """A span of one camera's timeline with one known classification."""

    start: datetime
    end: datetime
    status: str
    reason: str | None
    camera: str | None = None

    @property
    def is_gap(self) -> bool:
        return self.status in _GAP_STATUSES

    @property
    def is_unknown(self) -> bool:
        return self.status == HealthStatus.UNKNOWN

    def as_dict_typed(self) -> dict[str, object]:
        """Native types, for building a Pydantic view."""
        return {
            "start": self.start,
            "end": self.end,
            "status": self.status,
            "reason": self.reason,
            "meaning": meaning_of(self.reason),
            "camera": self.camera,
        }

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.status,
            "reason": self.reason,
            "meaning": meaning_of(self.reason),
        }
        if self.camera is not None:
            out["camera"] = self.camera
        return out


@dataclass
class Coverage:
    """The answer, in the shape the MCP tools hand to Hermes."""

    start: datetime
    end: datetime
    complete: bool | None
    cameras_considered: list[str]
    gaps: list[Segment] = field(default_factory=list)
    unknown_periods: list[Segment] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "complete": self.complete,
            "reason": self.reason,
            "cameras_considered": self.cameras_considered,
            "coverage_gaps": [g.as_dict() for g in self.gaps],
            "unknown_periods": [u.as_dict() for u in self.unknown_periods],
        }


def _verdict(gaps: list[Segment], unknowns: list[Segment]) -> bool | None:
    """A known gap outranks an unknown; both lists are still reported.

    A real outage does not erase the fact that other stretches were unverified,
    so ``complete: False`` still carries its unknown periods alongside.
    """
    if gaps:
        return False
    if unknowns:
        return None
    return True


def _merge(segments: list[Segment]) -> list[Segment]:
    """Coalesce adjacent segments sharing a status and reason.

    Purely cosmetic, and worth it: an interval split by a monitor restart would
    otherwise be reported as two abutting outages, which reads like two events.
    """
    merged: list[Segment] = []
    for seg in sorted(segments, key=lambda s: s.start):
        if seg.end <= seg.start:
            continue
        last = merged[-1] if merged else None
        if (
            last is not None
            and last.end == seg.start
            and last.status == seg.status
            and last.reason == seg.reason
            and last.camera == seg.camera
        ):
            last.end = seg.end
        else:
            merged.append(seg)
    return merged


async def camera_segments(
    repo: CameraHealthRepository,
    camera_key: str,
    *,
    start: datetime,
    end: datetime,
    grace_seconds: float = 0.0,
) -> list[Segment]:
    """One camera's timeline across [start, end], with every hole made explicit.

    This is where the honesty lives. An interval is only trusted as far as its
    ``observed_through``: the span between that and the next interval's start is
    time during which nobody was looking, and it becomes an explicit
    ``monitoring_gap`` rather than being absorbed into the surrounding status.

    ``grace_seconds`` is the one concession, and it applies only to the interval
    still open. Polling is periodic, so at any instant the last few seconds are
    always unconfirmed; without a grace window every query ending "now" would
    report unknown, and a field that is always unknown is a field readers learn
    to ignore. The grace is the same tolerance the monitor uses to decide
    whether observation lapsed, so the two agree by construction: any hole the
    monitor would have split on is a hole this reports.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    grace = timedelta(seconds=max(grace_seconds, 0.0))

    first_seen = await repo.first_observed_at(camera_key)
    if first_seen is None:
        return [
            Segment(start, end, HealthStatus.UNKNOWN, HealthReason.TRACKING_NOT_STARTED, camera_key)
        ]

    segments: list[Segment] = []

    # Anything before this camera was ever observed is unknown, permanently.
    # No outage row exists for last month because we were not there, which is
    # not the same as last month having been fine.
    if first_seen > start:
        segments.append(
            Segment(
                start,
                min(first_seen, end),
                HealthStatus.UNKNOWN,
                HealthReason.BEFORE_TRACKING,
                camera_key,
            )
        )

    cursor = max(start, first_seen)
    for interval in await repo.intervals_for(camera_key, start=start, end=end):
        began = ensure_utc(interval.started_at)
        if interval.ended_at is not None:
            confirmed_to = ensure_utc(interval.ended_at)
        else:
            confirmed_to = ensure_utc(interval.observed_through) + grace

        # A hole between the last confirmed observation and this interval: the
        # monitor was down, disabled, or restarted.
        if began > cursor:
            segments.append(
                Segment(
                    cursor,
                    min(began, end),
                    HealthStatus.UNKNOWN,
                    HealthReason.MONITORING_GAP,
                    camera_key,
                )
            )
            cursor = min(began, end)

        span_start = max(began, start)
        span_end = min(confirmed_to, end)
        if span_end > span_start:
            segments.append(
                Segment(span_start, span_end, interval.status, interval.reason, camera_key)
            )
            cursor = max(cursor, span_end)

    # Trailing hole: confirmed observation stopped before the window ended.
    if cursor < end:
        segments.append(
            Segment(cursor, end, HealthStatus.UNKNOWN, HealthReason.MONITORING_GAP, camera_key)
        )

    return _merge(segments)


async def get_camera_coverage(
    repo: CameraHealthRepository,
    camera_key: str,
    *,
    start: datetime,
    end: datetime,
    grace_seconds: float = 0.0,
) -> Coverage:
    """Was this specific camera working throughout [start, end]?

    Camera-level, so another camera covering the same zone is irrelevant here --
    that is what the zone query is for.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    segments = await camera_segments(
        repo, camera_key, start=start, end=end, grace_seconds=grace_seconds
    )
    gaps = [s for s in segments if s.is_gap]
    unknowns = [s for s in segments if s.is_unknown]
    return Coverage(
        start=start,
        end=end,
        complete=_verdict(gaps, unknowns),
        cameras_considered=[camera_key],
        gaps=gaps,
        unknown_periods=unknowns,
    )


async def get_zone_coverage(
    repo: CameraHealthRepository,
    cameras: CamerasConfig,
    zone_key: str,
    *,
    start: datetime,
    end: datetime,
    grace_seconds: float = 0.0,
) -> Coverage:
    """Was *something* watching this zone throughout [start, end]?

    Time-slice union by sweep line. For each elementary slice the zone is:

        covered  if at least one camera was healthy in it
        unknown  else if at least one camera was unknown in it
        a gap    otherwise

    So ``complete: True`` means **at least one configured camera relevant to
    this zone was operational throughout every time slice** -- not that all
    cameras were working (a zone can be complete with one camera dead the whole
    period), and not that the whole zone was visible. Field of view is a
    separate fact, reported separately, and may still be partial or none.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    considered = cameras_observing(cameras, zone_key)

    if not considered:
        # Nothing broke. Nothing was ever configured to watch here, which is a
        # decision about where cameras were mounted, not an operational outage.
        # Reporting a health gap would make a genuinely broken camera
        # indistinguishable from a wall nobody pointed one at.
        return Coverage(
            start=start,
            end=end,
            complete=None,
            cameras_considered=[],
            reason=HealthReason.NOT_APPLICABLE_NO_CAMERAS,
        )

    per_camera = {key: await camera_segments(repo, key, start=start, end=end) for key in considered}

    boundaries = {start, end}
    for segments in per_camera.values():
        for seg in segments:
            boundaries.add(max(seg.start, start))
            boundaries.add(min(seg.end, end))
    points = sorted(b for b in boundaries if start <= b <= end)

    slices: list[Segment] = []
    for left, right in pairwise(points):
        if right <= left:
            continue
        statuses: list[tuple[str, str | None, str]] = []
        for key, segments in per_camera.items():
            for seg in segments:
                if seg.start <= left and seg.end >= right:
                    statuses.append((seg.status, seg.reason, key))
                    break

        if any(s == HealthStatus.HEALTHY for s, _, _ in statuses):
            continue  # something was watching
        if any(s == HealthStatus.UNKNOWN for s, _, _ in statuses) or not statuses:
            reason = next(
                (r for s, r, _ in statuses if s == HealthStatus.UNKNOWN),
                HealthReason.MONITORING_GAP,
            )
            slices.append(Segment(left, right, HealthStatus.UNKNOWN, reason, None))
            continue

        # Every camera that covers this zone was down.
        status, reason, _ = statuses[0]
        down = sorted(k for s, _, k in statuses if s in _GAP_STATUSES)
        slices.append(Segment(left, right, status, reason, ", ".join(down)))

    merged = _merge(slices)
    gaps = [s for s in merged if s.is_gap]
    unknowns = [s for s in merged if s.is_unknown]
    return Coverage(
        start=start,
        end=end,
        complete=_verdict(gaps, unknowns),
        cameras_considered=considered,
        gaps=gaps,
        unknown_periods=unknowns,
    )


# --------------------------------------------------------------------------- #
# Event-pipeline coverage: were Home Assistant's events actually reaching us?
# --------------------------------------------------------------------------- #


@dataclass
class PipelineGap:
    """One Home Assistant trigger that produced no delivery here."""

    camera: str
    trigger_at: datetime
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "camera": self.camera,
            "ha_trigger_timestamp": self.trigger_at.isoformat(),
            "reason": self.reason,
            "meaning": meaning_of(self.reason),
        }


@dataclass
class PipelineCoverage:
    """Whether hermes-home was known to be receiving what Home Assistant sent.

    Independent of camera health. A camera can be perfectly healthy while every
    one of its events is lost in transit -- that is precisely what happened on
    2026-09-09 -- so an answer that reports only camera health is describing the
    wrong half of the system.
    """

    start: datetime
    end: datetime
    complete: bool | None
    cameras_considered: list[str]
    gaps: list[PipelineGap] = field(default_factory=list)
    unknown_periods: list[Segment] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "period": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "complete": self.complete,
            "reason": self.reason,
            "meaning": meaning_of(self.reason),
            "cameras_considered": self.cameras_considered,
            "delivery_gaps": [g.as_dict() for g in self.gaps],
            "unknown_periods": [u.as_dict() for u in self.unknown_periods],
        }


async def get_pipeline_coverage(
    repo: DeliveryGapRepository,
    cameras: CamerasConfig,
    *,
    start: datetime,
    end: datetime,
    camera_keys: list[str],
) -> PipelineCoverage:
    """Tri-state delivery coverage for a set of cameras over a period.

        true   reconciliation was running throughout, and every Home Assistant
               trigger in the interval had a matching delivery
        false  at least one trigger has no delivery -- events are missing
        null   reconciliation was not running, so nothing can be claimed

    A quiet interval with reconciliation running is ``true``, not unknown.
    Nothing needed delivering, and the mechanism that would have noticed was
    working; requiring traffic to prove health would make every quiet night
    indistinguishable from an outage.
    """
    start, end = ensure_utc(start), ensure_utc(end)
    reconcilable = [k for k in camera_keys if cameras.cameras.get(k, None) is not None]
    reconcilable = [k for k in reconcilable if cameras.cameras[k].reconcilable()]

    if not reconcilable:
        return PipelineCoverage(
            start=start,
            end=end,
            complete=None,
            cameras_considered=[],
            reason=HealthReason.TRACKING_NOT_STARTED,
        )

    gaps: list[PipelineGap] = []
    unknown: list[Segment] = []

    for key in reconcilable:
        state = await repo.get_state(key)
        if state is None:
            unknown.append(
                Segment(start, end, HealthStatus.UNKNOWN, HealthReason.TRACKING_NOT_STARTED, key)
            )
            continue

        first = ensure_utc(state.first_checked_at)
        through = ensure_utc(state.checked_through)
        if first > start:
            unknown.append(
                Segment(
                    start,
                    min(first, end),
                    HealthStatus.UNKNOWN,
                    HealthReason.BEFORE_TRACKING,
                    key,
                )
            )
        if through < end:
            unknown.append(
                Segment(
                    max(through, start),
                    end,
                    HealthStatus.UNKNOWN,
                    HealthReason.MONITORING_GAP,
                    key,
                )
            )

        for row in await repo.gaps_between(start=start, end=end, camera_key=key):
            gaps.append(
                PipelineGap(
                    camera=key,
                    trigger_at=ensure_utc(row.ha_trigger_at),
                    reason=row.reason,
                )
            )

    unknown = _merge([u for u in unknown if u.end > u.start])
    gaps.sort(key=lambda g: g.trigger_at)
    return PipelineCoverage(
        start=start,
        end=end,
        complete=False if gaps else (None if unknown else True),
        cameras_considered=reconcilable,
        gaps=gaps,
        unknown_periods=unknown,
    )
