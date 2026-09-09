"""Coverage as Hermes actually receives it.

The requirement these tests exist to protect: a caller must never be able to
read ``events: []`` as "nothing happened" without also being told whether
anything was watching. Making coverage a separate tool Hermes has to remember
to call would guarantee it is sometimes skipped -- and the time it is skipped is
the time it mattered.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from hermes_home.api.deps import AppState
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import CameraHealthInterval, HealthReason, HealthStatus
from hermes_home.vision.mock import MockVisionProvider


@pytest.fixture
def app_state(session_factory, settings, home_config, cameras_config, fake_ha) -> AppState:
    return AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        engine=None,
        session_factory=session_factory,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
        worker=None,
    )


@pytest.fixture
async def mcp_server(app_state: AppState):
    from hermes_home.mcp.server import create_mcp_server

    return create_mcp_server(app_state)


async def call(mcp_server, name: str, arguments: dict | None = None) -> dict:
    result = await mcp_server.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return result.structured_content


async def set_health(
    session_factory, camera_key: str, status: str, *, hours_back: float = 6.0
) -> None:
    """Give a camera one continuous interval covering the recent past."""
    now = now_utc()
    async with session_scope(session_factory) as session:
        session.add(
            CameraHealthInterval(
                camera_key=camera_key,
                status=status,
                reason=(
                    HealthReason.CAMERA_ENTITY_UNAVAILABLE
                    if status == HealthStatus.OFFLINE
                    else None
                ),
                started_at=now - timedelta(hours=hours_back),
                ended_at=None,
                observed_through=now + timedelta(minutes=5),
            )
        )


@pytest.fixture
async def stored_event(session_factory, settings, cameras_config, fake_ha):
    from hermes_home.core.ids import delivery_key
    from hermes_home.ingest.worker import IngestWorker
    from hermes_home.storage.repositories import DeliveryRepository

    moment = now_utc()
    async with session_scope(session_factory) as session:
        await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=delivery_key(
                source="home_assistant",
                event_type="camera.person_detected",
                source_entity_id="image.front_door_event_image",
                occurred_at=moment,
            ),
            correlation_id="coverage-test",
            raw_body={
                "event_type": "camera.person_detected",
                "camera": "front_door",
                "entity_id": "image.front_door_event_image",
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
    )
    await worker.drain_once()


def window() -> dict[str, str]:
    now = now_utc()
    return {
        "start_time": (now - timedelta(hours=4)).isoformat(),
        "end_time": now.isoformat(),
    }


# --------------------------------------------------------------------------- #
# The four cases a negative answer has to distinguish
# --------------------------------------------------------------------------- #


async def test_events_with_complete_coverage(mcp_server, session_factory, stored_event) -> None:
    await set_health(session_factory, "front_door", HealthStatus.HEALTHY)
    out = await call(mcp_server, "home_search_events", {**window(), "camera": "front_door"})

    assert out["count"] == 1
    assert out["coverage"]["complete"] is True
    assert out["coverage"]["coverage_gaps"] == []


async def test_no_events_with_complete_coverage_is_a_genuine_all_clear(
    mcp_server, session_factory
) -> None:
    await set_health(session_factory, "front_door", HealthStatus.HEALTHY)
    out = await call(mcp_server, "home_search_events", {**window(), "camera": "front_door"})

    assert out["count"] == 0
    assert out["coverage"]["complete"] is True


async def test_no_events_with_incomplete_coverage_is_not_an_all_clear(
    mcp_server, session_factory
) -> None:
    """The case this whole feature exists for."""
    await set_health(session_factory, "front_door", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_search_events", {**window(), "camera": "front_door"})

    assert out["count"] == 0
    assert out["coverage"]["complete"] is False
    gap = out["coverage"]["coverage_gaps"][0]
    assert gap["status"] == HealthStatus.OFFLINE
    assert gap["reason"] == HealthReason.CAMERA_ENTITY_UNAVAILABLE


async def test_events_with_incomplete_coverage_disclose_both(
    mcp_server, session_factory, stored_event
) -> None:
    await set_health(session_factory, "front_door", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_search_events", {**window(), "camera": "front_door"})

    assert out["count"] == 1
    assert out["coverage"]["complete"] is False


async def test_a_period_before_tracking_is_unknown_never_complete(
    mcp_server, session_factory
) -> None:
    """No health data for last month, because nobody was watching last month."""
    await set_health(session_factory, "front_door", HealthStatus.HEALTHY)
    now = now_utc()
    out = await call(
        mcp_server,
        "home_search_events",
        {
            "start_time": (now - timedelta(days=30)).isoformat(),
            "end_time": (now - timedelta(days=29)).isoformat(),
            "camera": "front_door",
        },
    )
    assert out["coverage"]["complete"] is None
    assert out["coverage"]["unknown_periods"][0]["reason"] == HealthReason.BEFORE_TRACKING


# --------------------------------------------------------------------------- #
# Where coverage is attached
# --------------------------------------------------------------------------- #


async def test_recent_events_attaches_coverage_for_a_camera(mcp_server, session_factory) -> None:
    await set_health(session_factory, "front_door", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_recent_events", {"camera": "front_door", "minutes": 120})
    assert out["coverage"]["complete"] is False


async def test_recent_events_attaches_coverage_for_a_zone(mcp_server, session_factory) -> None:
    await set_health(session_factory, "backyard", HealthStatus.OFFLINE)
    await set_health(session_factory, "cottage", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_recent_events", {"zone": "backyard", "minutes": 120})
    assert out["coverage"]["complete"] is False
    assert set(out["coverage"]["cameras_considered"]) == {"backyard", "cottage"}


async def test_an_unfiltered_query_gets_no_coverage_block(mcp_server) -> None:
    """Coverage of 'everywhere' is not a question with a determinate answer."""
    out = await call(mcp_server, "home_recent_events", {"minutes": 60})
    assert "coverage" not in out


async def test_summarize_activity_carries_coverage(mcp_server, session_factory) -> None:
    await set_health(session_factory, "front_door", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_summarize_activity", {**window(), "zone": "front_entry"})

    assert out["event_count"] == 0
    assert out["coverage"]["complete"] is False
    # Field of view stays a separate fact.
    assert out["zone_coverage"]["status"] == "full"


async def test_field_of_view_and_operational_coverage_stay_separate(
    mcp_server, session_factory
) -> None:
    """The backyard: two cameras, each seeing only part of it.

    Both healthy means something was watching the whole time. It does NOT mean
    the whole yard was visible, and the two facts must not be merged.
    """
    await set_health(session_factory, "backyard", HealthStatus.HEALTHY)
    await set_health(session_factory, "cottage", HealthStatus.HEALTHY)
    out = await call(mcp_server, "home_search_events", {**window(), "zone": "backyard"})

    assert out["coverage"]["complete"] is True
    assert out["coverage"]["field_of_view"]["status"] == "partial"
    assert out["zone_coverage"]["status"] == "partial"


# --------------------------------------------------------------------------- #
# The dedicated tools
# --------------------------------------------------------------------------- #


async def test_list_cameras_reports_health_for_every_camera(
    mcp_server, session_factory, settings, cameras_config, fake_ha
) -> None:
    from tests.test_camera_health import ScriptedHA, monitor

    ha = ScriptedHA(states={"camera.backyard": "unavailable"})
    settings.camera_health_failure_threshold = 1
    await monitor(session_factory, settings, cameras_config, ha).poll_once()

    out = await call(mcp_server, "home_list_cameras")
    assert out["count"] == len(cameras_config.cameras)
    by_key = {c["key"]: c for c in out["cameras"]}
    assert by_key["backyard"]["status"] == HealthStatus.OFFLINE
    assert by_key["front_door"]["status"] == HealthStatus.HEALTHY
    assert out["by_status"][HealthStatus.OFFLINE] == 1


async def test_list_cameras_never_seen_reports_unknown_not_healthy(mcp_server) -> None:
    out = await call(mcp_server, "home_list_cameras", {"camera": "front_door"})
    camera = out["cameras"][0]
    assert camera["status"] == HealthStatus.UNKNOWN
    assert camera["reason"] == HealthReason.TRACKING_NOT_STARTED
    assert camera["checked_at"] is None


async def test_list_cameras_last_event_is_not_a_health_signal(
    mcp_server, session_factory, stored_event
) -> None:
    await set_health(session_factory, "front_door", HealthStatus.HEALTHY)
    out = await call(mcp_server, "home_list_cameras", {"camera": "front_door"})
    camera = out["cameras"][0]
    assert camera["last_event_at"] is not None
    # Recorded events do not make current health known: history says the camera
    # was working, not that it is. Only a health poll can say that.
    assert camera["status"] == HealthStatus.UNKNOWN

    quiet = {c["key"]: c for c in (await call(mcp_server, "home_list_cameras"))["cameras"]}
    # A camera with no events at all is not thereby unhealthy.
    assert quiet["right_walkway"]["last_event_at"] is None


async def test_home_coverage_for_a_camera(mcp_server, session_factory) -> None:
    await set_health(session_factory, "front_door", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_coverage", {**window(), "camera": "front_door"})
    assert out["complete"] is False
    assert out["cameras_considered"] == ["front_door"]


async def test_home_coverage_for_a_zone_includes_field_of_view(mcp_server, session_factory) -> None:
    await set_health(session_factory, "garage_left", HealthStatus.HEALTHY)
    await set_health(session_factory, "garage_right", HealthStatus.OFFLINE)
    out = await call(mcp_server, "home_coverage", {**window(), "zone": "driveway"})
    assert out["complete"] is True
    assert out["field_of_view"]["status"] == "full"


async def test_home_coverage_requires_exactly_one_of_camera_or_zone(mcp_server) -> None:
    for arguments in ({}, {"camera": "front_door", "zone": "front_entry"}):
        out = await call(mcp_server, "home_coverage", {**window(), **arguments})
        # An actionable message, not a hidden exception: a caller that cannot
        # see what it got wrong cannot correct it.
        assert "exactly one" in out["error"]


async def test_describe_home_separates_field_of_view_from_health(
    mcp_server, session_factory, settings, cameras_config
) -> None:
    from tests.test_camera_health import ScriptedHA, monitor

    ha = ScriptedHA(states={"camera.backyard": "unavailable"})
    settings.camera_health_failure_threshold = 1
    await monitor(session_factory, settings, cameras_config, ha).poll_once()

    out = await call(mcp_server, "home_describe_home")
    backyard = next(c for c in out["cameras"] if c["key"] == "backyard")

    # Where it points is unchanged by whether it works.
    assert "backyard" in backyard["observes"]
    assert backyard["partial_coverage"] == ["backyard"]
    # And its health is a separate field.
    assert backyard["current_health"] == HealthStatus.OFFLINE
    assert backyard["health_checked_at"] is not None
