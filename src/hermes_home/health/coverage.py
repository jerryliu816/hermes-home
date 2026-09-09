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
from hermes_home.storage.models import HealthReason, HealthStatus
from hermes_home.storage.repositories import CameraHealthRepository

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
            "camera": self.camera,
        }

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "status": self.status,
            "reason": self.reason,
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
