"""query_context: today's date and where the recorded history begins.

Measured, not hypothetical: asked about the evening of October 31 2026, the
Hermes agent searched October 31 2025. It got zero events and a coverage block
saying health was "before_health_tracking" -- all true, and none of it pointing
at the wrong year. These tests pin down that every time-bounded tool now says
what today is and when recording began, adds a plain note when the period is
outside that history, and changes nothing else about the answer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from hermes_home.api.deps import AppState
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import create_verification_engine, session_scope
from hermes_home.storage.models import CameraHealthInterval, Event, HealthStatus
from hermes_home.vision.mock import MockVisionProvider


@pytest.fixture
def app_state(session_factory, settings, home_config, cameras_config, fake_ha) -> AppState:
    settings.display_timezone = "America/Los_Angeles"
    return AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        engine=create_verification_engine(settings.database_url),
        verify_engine=create_verification_engine(settings.database_url),
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


async def _store_event(session_factory, settings, cameras_config, fake_ha) -> datetime:
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
            correlation_id="query-context-test",
            raw_body={
                "event_type": "camera.person_detected",
                "camera": "front_door",
                "entity_id": "image.front_door_event_image",
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
    await IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
    ).drain_once()
    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        assert event is not None
        return moment


async def _track_health(session_factory, *, since: datetime) -> None:
    async with session_scope(session_factory) as session:
        session.add(
            CameraHealthInterval(
                camera_key="front_door",
                status=HealthStatus.HEALTHY,
                reason=None,
                started_at=since,
                ended_at=None,
                observed_through=now_utc() + timedelta(minutes=5),
            )
        )


@pytest.fixture
async def history(session_factory, settings, cameras_config, fake_ha) -> datetime:
    """Health tracked for six hours, and one event just now. Returns tracking start."""
    since = now_utc() - timedelta(hours=6)
    await _track_health(session_factory, since=since)
    await _store_event(session_factory, settings, cameras_config, fake_ha)
    return since


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _year_earlier_window() -> dict[str, str]:
    end = now_utc() - timedelta(days=365)
    return {"start_time": _iso(end - timedelta(hours=7)), "end_time": _iso(end)}


# --------------------------------------------------------------------------- #


async def test_every_time_bounded_tool_carries_query_context(mcp_server, history) -> None:
    now = now_utc()
    window = {"start_time": _iso(now - timedelta(hours=2)), "end_time": _iso(now)}
    results = {
        "home_recent_events": await call(mcp_server, "home_recent_events"),
        "home_search_events": await call(mcp_server, "home_search_events", window),
        "home_summarize_activity": await call(mcp_server, "home_summarize_activity", window),
        "home_coverage": await call(
            mcp_server, "home_coverage", {**window, "camera": "front_door"}
        ),
    }
    for name, out in results.items():
        context = out["query_context"]
        assert context["timezone"] == "America/Los_Angeles", name
        assert str(now.year) in context["today"], name
        assert context["now_local"].endswith(("-07:00", "-08:00")), name
        assert context["first_event_at"] is not None, name
        assert context["health_tracking_from"] is not None, name
        assert "note" not in context, name


async def test_a_period_in_the_right_year_gets_no_note(mcp_server, history) -> None:
    now = now_utc()
    out = await call(
        mcp_server,
        "home_search_events",
        {
            "camera": "front_door",
            "start_time": _iso(now - timedelta(hours=1)),
            "end_time": _iso(now + timedelta(minutes=1)),
        },
    )
    assert out["count"] == 1
    assert "note" not in out["query_context"]


async def test_a_year_early_search_says_so_and_names_today(mcp_server, history) -> None:
    out = await call(
        mcp_server, "home_search_events", {"camera": "front_door", **_year_earlier_window()}
    )
    note = out["query_context"]["note"]
    assert "Nothing is recorded for this period" in note
    assert "check the year" in note
    assert out["query_context"]["today"] in note
    assert str(now_utc().year) in note


async def test_the_note_changes_nothing_else(mcp_server, history) -> None:
    """The existing unknown-coverage answer must stay exactly what it was."""
    out = await call(
        mcp_server, "home_search_events", {"camera": "front_door", **_year_earlier_window()}
    )
    assert out["count"] == 0
    assert out["events"] == []
    coverage = out["camera_health_coverage"]
    assert coverage["complete"] is None
    assert [p["reason"] for p in coverage["unknown_periods"]] == ["before_health_tracking"]


async def test_coverage_and_summary_also_flag_a_year_early_period(mcp_server, history) -> None:
    window = _year_earlier_window()
    coverage = await call(mcp_server, "home_coverage", {"camera": "front_door", **window})
    summary = await call(mcp_server, "home_summarize_activity", window)
    assert "note" in coverage["query_context"]
    assert "note" in summary["query_context"]
    assert summary["event_count"] == 0


async def test_a_future_period_is_flagged(mcp_server, history) -> None:
    start = now_utc() + timedelta(days=365)
    out = await call(
        mcp_server,
        "home_search_events",
        {"start_time": _iso(start), "end_time": _iso(start + timedelta(hours=7))},
    )
    assert "after the current time" in out["query_context"]["note"]


async def test_a_tracked_but_quiet_period_is_not_flagged(
    mcp_server, session_factory, settings, cameras_config, fake_ha
) -> None:
    """Health was recorded before the first event: an empty window there is real."""
    await _track_health(session_factory, since=now_utc() - timedelta(days=3))
    await _store_event(session_factory, settings, cameras_config, fake_ha)
    end = now_utc() - timedelta(days=2)
    out = await call(
        mcp_server,
        "home_search_events",
        {
            "camera": "front_door",
            "start_time": _iso(end - timedelta(hours=4)),
            "end_time": _iso(end),
        },
    )
    assert out["count"] == 0
    assert "note" not in out["query_context"]


async def test_with_no_history_at_all_nothing_is_claimed(mcp_server) -> None:
    out = await call(mcp_server, "home_search_events", _year_earlier_window())
    context = out["query_context"]
    assert context["first_event_at"] is None
    assert context["health_tracking_from"] is None
    assert "note" not in context


async def test_tool_descriptions_tell_the_model_what_the_note_means(mcp_server) -> None:
    tools = {t.name: t for t in await mcp_server.list_tools()}
    for name in ("home_search_events", "home_summarize_activity", "home_coverage"):
        assert "query_context" in tools[name].description, name
        assert "wrong year" in tools[name].description, name
