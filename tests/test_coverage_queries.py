"""Historical coverage.

Every test here is a variation on one question: when may we say a period was
covered? The answer must be "only when we actually watched it", and the failure
mode to guard against is a confident `true` produced by the absence of evidence
rather than by evidence of absence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hermes_home.core.time import now_utc
from hermes_home.health.coverage import get_camera_coverage, get_zone_coverage
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import CameraHealthInterval, HealthReason, HealthStatus

BASE = datetime(2026, 9, 9, 0, 0, tzinfo=ZoneInfo("UTC"))


def at(hours: float) -> datetime:
    return BASE + timedelta(hours=hours)


async def record(
    session_factory,
    camera_key: str,
    spans: list[tuple[float, float | None, str]],
    *,
    reason: str | None = None,
) -> None:
    """Write health intervals directly: (start_hour, end_hour, status).

    ``end_hour`` None leaves the interval open, confirmed only through its own
    start -- which is what a monitor that stopped looks like.
    """
    async with session_scope(session_factory) as session:
        for start_h, end_h, status in spans:
            end = at(end_h) if end_h is not None else None
            session.add(
                CameraHealthInterval(
                    camera_key=camera_key,
                    status=status,
                    reason=reason
                    or (
                        HealthReason.CAMERA_ENTITY_UNAVAILABLE
                        if status == HealthStatus.OFFLINE
                        else None
                    ),
                    started_at=at(start_h),
                    ended_at=end,
                    observed_through=end if end is not None else at(start_h),
                )
            )


async def camera_coverage(session_factory, key: str, start: float, end: float):
    from hermes_home.storage.repositories import CameraHealthRepository

    async with session_scope(session_factory) as session:
        return await get_camera_coverage(
            CameraHealthRepository(session), key, start=at(start), end=at(end)
        )


async def zone_coverage(session_factory, cameras, zone: str, start: float, end: float):
    from hermes_home.storage.repositories import CameraHealthRepository

    async with session_scope(session_factory) as session:
        return await get_zone_coverage(
            CameraHealthRepository(session), cameras, zone, start=at(start), end=at(end)
        )


# --------------------------------------------------------------------------- #
# The base case, and the shapes an outage can take relative to a query
# --------------------------------------------------------------------------- #


async def test_a_fully_healthy_interval_is_complete(session_factory) -> None:
    await record(session_factory, "front_door", [(0, 12, HealthStatus.HEALTHY)])
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is True
    assert result.gaps == []
    assert result.unknown_periods == []


async def test_an_overlapping_outage_makes_coverage_incomplete(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [
            (0, 3.283, HealthStatus.HEALTHY),
            (3.283, 5.7, HealthStatus.OFFLINE),
            (5.7, 12, HealthStatus.HEALTHY),
        ],
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert len(result.gaps) == 1
    assert result.gaps[0].reason == HealthReason.CAMERA_ENTITY_UNAVAILABLE


async def test_an_outage_entirely_before_the_query_is_irrelevant(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [(0, 1, HealthStatus.OFFLINE), (1, 12, HealthStatus.HEALTHY)],
    )
    assert (await camera_coverage(session_factory, "front_door", 2, 6)).complete is True


async def test_an_outage_entirely_after_the_query_is_irrelevant(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [(0, 8, HealthStatus.HEALTHY), (8, 12, HealthStatus.OFFLINE)],
    )
    assert (await camera_coverage(session_factory, "front_door", 2, 6)).complete is True


async def test_an_outage_starting_inside_the_query(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [(0, 4, HealthStatus.HEALTHY), (4, 12, HealthStatus.OFFLINE)],
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert result.gaps[0].start == at(4)
    assert result.gaps[0].end == at(6)  # clipped to the query


async def test_an_outage_ending_inside_the_query(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [(0, 3, HealthStatus.OFFLINE), (3, 12, HealthStatus.HEALTHY)],
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert result.gaps[0].start == at(2)
    assert result.gaps[0].end == at(3)


async def test_an_outage_spanning_the_entire_query(session_factory) -> None:
    await record(session_factory, "front_door", [(0, 12, HealthStatus.OFFLINE)])
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert result.gaps[0].start == at(2)
    assert result.gaps[0].end == at(6)


async def test_multiple_outages_are_all_reported(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [
            (0, 2.5, HealthStatus.HEALTHY),
            (2.5, 3, HealthStatus.OFFLINE),
            (3, 4, HealthStatus.HEALTHY),
            (4, 4.5, HealthStatus.OFFLINE),
            (4.5, 12, HealthStatus.HEALTHY),
        ],
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert len(result.gaps) == 2


async def test_degraded_counts_as_a_gap(session_factory) -> None:
    """A camera whose image entity is down would produce no analyzable frame."""
    await record(
        session_factory,
        "front_door",
        [
            (0, 3, HealthStatus.HEALTHY),
            (3, 4, HealthStatus.DEGRADED),
            (4, 12, HealthStatus.HEALTHY),
        ],
        reason=HealthReason.EVENT_IMAGE_ENTITY_UNAVAILABLE,
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert result.gaps[0].status == HealthStatus.DEGRADED
    # Distinguishable from a true outage, so the difference can be explained.
    assert result.gaps[0].reason == HealthReason.EVENT_IMAGE_ENTITY_UNAVAILABLE


async def test_an_unknown_interval_is_not_a_gap(session_factory) -> None:
    """Home Assistant being unreachable is our blindness, not a camera fault."""
    await record(
        session_factory,
        "front_door",
        [
            (0, 3, HealthStatus.HEALTHY),
            (3, 4, HealthStatus.UNKNOWN),
            (4, 12, HealthStatus.HEALTHY),
        ],
        reason=HealthReason.HA_UNREACHABLE,
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is None
    assert result.gaps == []
    assert result.unknown_periods[0].reason == HealthReason.HA_UNREACHABLE


async def test_a_known_gap_outranks_an_unknown_but_both_are_reported(session_factory) -> None:
    await record(
        session_factory,
        "front_door",
        [
            (0, 3, HealthStatus.UNKNOWN),
            (3, 4, HealthStatus.OFFLINE),
            (4, 12, HealthStatus.HEALTHY),
        ],
    )
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is False
    assert result.gaps and result.unknown_periods


# --------------------------------------------------------------------------- #
# The observation boundary: no fabricated history
# --------------------------------------------------------------------------- #


async def test_a_query_before_tracking_began_is_unknown_not_complete(session_factory) -> None:
    """The single most important test in this file.

    There is no outage row for last month because nobody was watching last
    month. Reading that as health would be inventing evidence.
    """
    await record(session_factory, "front_door", [(10, 20, HealthStatus.HEALTHY)])
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is None
    assert result.unknown_periods[0].reason == HealthReason.BEFORE_TRACKING


async def test_a_camera_never_monitored_at_all_is_unknown(session_factory) -> None:
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is None
    assert result.unknown_periods[0].reason == HealthReason.TRACKING_NOT_STARTED


async def test_a_query_straddling_the_boundary_reports_both_halves(session_factory) -> None:
    await record(session_factory, "front_door", [(4, 12, HealthStatus.HEALTHY)])
    result = await camera_coverage(session_factory, "front_door", 2, 6)
    assert result.complete is None
    assert result.unknown_periods[0].start == at(2)
    assert result.unknown_periods[0].end == at(4)


async def test_service_downtime_stays_unknown_even_when_healthy_either_side(
    session_factory,
) -> None:
    """Service down 01:00-05:00, camera healthy before and after.

    The two healthy spans must NOT be stitched together: being healthy either
    side of an outage says nothing whatsoever about the middle.
    """
    await record(
        session_factory,
        "front_door",
        [(0, 1, HealthStatus.HEALTHY), (5, 12, HealthStatus.HEALTHY)],
    )
    result = await camera_coverage(session_factory, "front_door", 0, 12)
    assert result.complete is None
    assert len(result.unknown_periods) == 1
    gap = result.unknown_periods[0]
    assert gap.start == at(1)
    assert gap.end == at(5)
    assert gap.reason == HealthReason.MONITORING_GAP


async def test_a_monitor_that_stopped_does_not_extend_its_last_interval(
    session_factory,
) -> None:
    """An open interval is trusted only as far as observed_through."""
    await record(session_factory, "front_door", [(0, None, HealthStatus.HEALTHY)])
    result = await camera_coverage(session_factory, "front_door", 0, 6)
    assert result.complete is None
    assert result.unknown_periods[0].reason == HealthReason.MONITORING_GAP
    assert result.unknown_periods[0].end == at(6)


async def test_monitor_restart_with_unchanged_status_preserves_the_gap(
    session_factory, settings
) -> None:
    """The split must happen even though nothing about the camera changed."""
    from hermes_home.storage.repositories import CameraHealthRepository
    from tests.test_camera_health import ScriptedHA, monitor, one_camera

    mon = monitor(session_factory, settings, one_camera(), ScriptedHA())
    await mon.poll_once()

    # A second monitor: a restarted process, same healthy camera.
    restarted = monitor(session_factory, settings, one_camera(), ScriptedHA())
    await restarted.poll_once()

    async with session_scope(session_factory) as session:
        intervals = await CameraHealthRepository(session).intervals_for(
            "test",
            start=BASE - timedelta(days=3650),
            end=BASE + timedelta(days=3650),
        )
    assert len(intervals) == 2, "startup must split, not extend"
    assert intervals[0].status == intervals[1].status == HealthStatus.HEALTHY
    assert intervals[0].ended_at is not None


async def test_one_observed_failure_survives_debouncing_in_history(
    session_factory, settings
) -> None:
    """A single unavailable reading, then recovery.

    Current status never leaves healthy -- that is what the threshold is for.
    But the outage was genuinely observed, so a historical query over it must
    not report complete coverage.
    """
    from hermes_home.storage.repositories import CameraHealthRepository
    from tests.test_camera_health import ScriptedHA, monitor, one_camera

    settings.camera_health_failure_threshold = 3
    ha = ScriptedHA()
    mon = monitor(session_factory, settings, one_camera(), ha)

    await mon.poll_once()
    ha.states["camera.test"] = "unavailable"
    await mon.poll_once()
    ha.states.pop("camera.test")
    await mon.poll_once()

    async with session_scope(session_factory) as session:
        repo = CameraHealthRepository(session)
        row = await repo.get("test")
        result = await get_camera_coverage(
            repo, "test", start=now_utc() - timedelta(minutes=5), end=now_utc()
        )

    assert row.status == HealthStatus.HEALTHY, "debounce should spare the current status"
    assert result.complete is False, "but the observed failure must remain in history"
    assert any(g.status == HealthStatus.OFFLINE for g in result.gaps)


# --------------------------------------------------------------------------- #
# Boundaries and time zones
# --------------------------------------------------------------------------- #


async def test_exact_boundary_timestamps(session_factory) -> None:
    """An outage abutting the query edge exactly must not leak into it."""
    await record(
        session_factory,
        "front_door",
        [
            (0, 2, HealthStatus.OFFLINE),
            (2, 6, HealthStatus.HEALTHY),
            (6, 12, HealthStatus.OFFLINE),
        ],
    )
    assert (await camera_coverage(session_factory, "front_door", 2, 6)).complete is True


async def test_a_local_time_query_across_a_dst_change(session_factory) -> None:
    """US DST ends 2026-11-01; 01:00-03:00 local is three real hours."""
    from hermes_home.storage.repositories import CameraHealthRepository

    pacific = ZoneInfo("America/Los_Angeles")
    start = datetime(2026, 11, 1, 1, 0, tzinfo=pacific)
    end = datetime(2026, 11, 1, 3, 0, tzinfo=pacific)
    # Wall-clock subtraction says two hours; the real elapsed time is three,
    # which is precisely why every stored timestamp is UTC and every comparison
    # happens after conversion.
    assert (end - start).total_seconds() == 2 * 3600
    real_elapsed = end.astimezone(UTC) - start.astimezone(UTC)
    assert real_elapsed.total_seconds() == 3 * 3600

    async with session_scope(session_factory) as session:
        session.add(
            CameraHealthInterval(
                camera_key="front_door",
                status=HealthStatus.HEALTHY,
                reason=None,
                started_at=(start - timedelta(hours=1)).astimezone(UTC),
                ended_at=None,
                # Confirmed for two of the three real hours, so the last one
                # must come back unknown rather than being absorbed.
                observed_through=(start.astimezone(UTC) + timedelta(hours=2)),
            )
        )
    async with session_scope(session_factory) as session:
        result = await get_camera_coverage(
            CameraHealthRepository(session), "front_door", start=start, end=end
        )

    # The last real hour was never confirmed, so it is unknown -- not healthy.
    assert result.complete is None
    assert result.unknown_periods[0].reason == HealthReason.MONITORING_GAP


# --------------------------------------------------------------------------- #
# Zones: several cameras, one place
# --------------------------------------------------------------------------- #


async def test_a_zone_is_covered_while_any_camera_is_healthy(
    session_factory, cameras_config
) -> None:
    """driveway is watched by garage_left and garage_right."""
    await record(
        session_factory,
        "garage_right",
        [(0, 3, HealthStatus.HEALTHY), (3, 4, HealthStatus.OFFLINE), (4, 12, HealthStatus.HEALTHY)],
    )
    await record(session_factory, "garage_left", [(0, 12, HealthStatus.HEALTHY)])

    zone = await zone_coverage(session_factory, cameras_config, "driveway", 2, 6)
    assert zone.complete is True
    assert set(zone.cameras_considered) == {"garage_left", "garage_right"}

    # The camera-specific question still gets the camera-specific answer.
    camera = await camera_coverage(session_factory, "garage_right", 2, 6)
    assert camera.complete is False


async def test_one_camera_offline_the_whole_interval_still_leaves_the_zone_covered(
    session_factory, cameras_config
) -> None:
    await record(session_factory, "garage_right", [(0, 12, HealthStatus.OFFLINE)])
    await record(session_factory, "garage_left", [(0, 12, HealthStatus.HEALTHY)])

    zone = await zone_coverage(session_factory, cameras_config, "driveway", 2, 6)
    assert zone.complete is True
    assert zone.gaps == []

    camera = await camera_coverage(session_factory, "garage_right", 2, 6)
    assert camera.complete is False
    assert camera.gaps[0].start == at(2)
    assert camera.gaps[0].end == at(6)


async def test_a_zone_with_every_camera_offline_is_a_gap(session_factory, cameras_config) -> None:
    await record(session_factory, "garage_right", [(0, 12, HealthStatus.OFFLINE)])
    await record(session_factory, "garage_left", [(0, 12, HealthStatus.OFFLINE)])

    zone = await zone_coverage(session_factory, cameras_config, "driveway", 2, 6)
    assert zone.complete is False
    assert zone.gaps[0].start == at(2)


async def test_a_zone_partially_covered_in_time_reports_only_the_uncovered_part(
    session_factory, cameras_config
) -> None:
    await record(
        session_factory,
        "garage_right",
        [(0, 3, HealthStatus.HEALTHY), (3, 12, HealthStatus.OFFLINE)],
    )
    await record(
        session_factory,
        "garage_left",
        [(0, 4, HealthStatus.OFFLINE), (4, 12, HealthStatus.HEALTHY)],
    )
    zone = await zone_coverage(session_factory, cameras_config, "driveway", 2, 6)
    assert zone.complete is False
    assert len(zone.gaps) == 1
    assert zone.gaps[0].start == at(3)
    assert zone.gaps[0].end == at(4)


async def test_a_zone_no_camera_watches_is_not_an_operational_outage(
    session_factory, cameras_config
) -> None:
    """Nothing broke. Nothing was ever configured to watch there.

    Reporting a health gap would make a genuinely broken camera
    indistinguishable from a wall nobody pointed one at.
    """
    zone = await zone_coverage(session_factory, cameras_config, "garage", 2, 6)
    assert zone.complete is None
    assert zone.reason == HealthReason.NOT_APPLICABLE_NO_CAMERAS
    assert zone.gaps == []
    assert zone.cameras_considered == []


# --------------------------------------------------------------------------- #
# The polling-latency grace, and its limit
# --------------------------------------------------------------------------- #


async def test_the_seconds_since_the_last_poll_are_not_reported_as_a_gap(
    session_factory, settings
) -> None:
    """Polling is periodic, so the last few seconds are always unconfirmed.

    Without a grace window every query ending "now" would return unknown, and a
    field that is always unknown is one readers learn to ignore.
    """
    from hermes_home.storage.repositories import CameraHealthRepository

    now = now_utc()
    async with session_scope(session_factory) as session:
        session.add(
            CameraHealthInterval(
                camera_key="front_door",
                status=HealthStatus.HEALTHY,
                reason=None,
                started_at=now - timedelta(hours=1),
                ended_at=None,
                observed_through=now - timedelta(seconds=20),
            )
        )

    async with session_scope(session_factory) as session:
        result = await get_camera_coverage(
            CameraHealthRepository(session),
            "front_door",
            start=now - timedelta(minutes=30),
            end=now,
            grace_seconds=settings.camera_health_gap_tolerance_seconds,
        )
    assert result.complete is True


async def test_the_grace_does_not_cover_a_monitor_that_actually_stopped(
    session_factory, settings
) -> None:
    """One missed cadence is latency; an hour of silence is a gap.

    The grace is exactly the tolerance the monitor uses to decide observation
    lapsed, so the two agree by construction.
    """
    from hermes_home.storage.repositories import CameraHealthRepository

    now = now_utc()
    async with session_scope(session_factory) as session:
        session.add(
            CameraHealthInterval(
                camera_key="front_door",
                status=HealthStatus.HEALTHY,
                reason=None,
                started_at=now - timedelta(hours=4),
                ended_at=None,
                observed_through=now - timedelta(hours=1),
            )
        )

    async with session_scope(session_factory) as session:
        result = await get_camera_coverage(
            CameraHealthRepository(session),
            "front_door",
            start=now - timedelta(hours=2),
            end=now,
            grace_seconds=settings.camera_health_gap_tolerance_seconds,
        )
    assert result.complete is None
    assert result.unknown_periods[0].reason == HealthReason.MONITORING_GAP
