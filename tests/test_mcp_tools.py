"""The MCP tool surface.

These names and field shapes are an external contract that Hermes' config and
prompts depend on, so the tests assert the contract, not just that code runs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from hermes_home.api.deps import AppState
from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Event
from hermes_home.vision.mock import MockVisionProvider

EXPECTED_TOOLS = {
    "home_recent_events",
    "home_search_events",
    "home_get_event",
    "home_list_zones",
    "home_describe_home",
    "home_summarize_activity",
}


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


@pytest.fixture
async def stored_event(session_factory, settings, cameras_config, fake_ha):
    """One realistic event, shaped like the real Front Door capture."""
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
            correlation_id="mcp-test",
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

    async with session_scope(session_factory) as session:
        return (await session.scalar(select(Event))).uid


async def call(mcp_server, name: str, arguments: dict | None = None) -> dict:
    """Invoke a tool the way a client would, through the server's dispatcher."""
    result = await mcp_server.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    return result.structured_content


# --------------------------------------------------------------------------- #
# Contract
# --------------------------------------------------------------------------- #


async def test_exactly_the_intended_tools_are_exposed(mcp_server) -> None:
    names = {t.name for t in await mcp_server.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_no_home_assistant_passthrough_is_exposed(mcp_server) -> None:
    """Hermes already owns current state and device control; we must not duplicate it."""
    names = {t.name for t in await mcp_server.list_tools()}
    forbidden = {"ha_get_state", "ha_list_entities", "ha_call_service", "get_entity_state"}
    assert names & forbidden == set()
    assert not any("sql" in n.lower() or "query_db" in n.lower() for n in names)


async def test_every_tool_is_described_for_a_model(mcp_server) -> None:
    for tool in await mcp_server.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name
        assert tool.input_schema is not None


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


async def test_recent_events_returns_the_stored_event(mcp_server, stored_event) -> None:
    out = await call(mcp_server, "home_recent_events", {"minutes": 60})

    assert out["count"] == 1
    event = out["events"][0]
    assert event["uid"] == stored_event
    assert event["event_type"] == "camera.person_detected"
    assert event["camera"] == "front_door"
    assert event["zone"] == "front_entry"
    assert event["summary"]


async def test_events_carry_local_time_alongside_utc(mcp_server, stored_event) -> None:
    event = (await call(mcp_server, "home_recent_events", {"minutes": 60}))["events"][0]
    assert event["occurred_at"].endswith("Z") or "+00:00" in event["occurred_at"]
    assert event["occurred_at_local"] != event["occurred_at"]


async def test_unknown_values_are_explicit_nulls(mcp_server, stored_event) -> None:
    """An omitted key reads as zero to a language model; a null does not."""
    event = (await call(mcp_server, "home_recent_events", {"minutes": 60}))["events"][0]
    observation = event["analysis"]["observation"]

    assert "vehicle_count" in observation
    assert observation["vehicle_count"] is None
    assert observation["person_count"] == 0 or observation["person_count"] >= 1


async def test_internal_ids_are_never_exposed(mcp_server, stored_event) -> None:
    event = (await call(mcp_server, "home_recent_events", {"minutes": 60}))["events"][0]
    assert "id" not in event
    assert "zone_id" not in event
    assert "event_id" not in event


async def test_get_event_by_uid(mcp_server, stored_event) -> None:
    out = await call(mcp_server, "home_get_event", {"uid": stored_event})
    assert out["found"] is True
    assert out["event"]["uid"] == stored_event


async def test_get_event_missing_uid_is_not_an_error(mcp_server) -> None:
    out = await call(mcp_server, "home_get_event", {"uid": "does-not-exist"})
    assert out["found"] is False
    assert out["event"] is None


async def test_search_filters_by_camera_and_zone(mcp_server, stored_event) -> None:
    assert (await call(mcp_server, "home_search_events", {"camera": "front_door"}))["count"] == 1
    assert (await call(mcp_server, "home_search_events", {"camera": "nope"}))["count"] == 0
    assert (await call(mcp_server, "home_search_events", {"zone": "front_entry"}))["count"] == 1
    assert (await call(mcp_server, "home_search_events", {"zone": "backyard"}))["count"] == 0


async def test_search_filters_by_type_prefix_and_tags(mcp_server, stored_event) -> None:
    assert (await call(mcp_server, "home_search_events", {"event_type": "camera."}))["count"] == 1
    assert (await call(mcp_server, "home_search_events", {"event_type": "energy."}))["count"] == 0
    assert (await call(mcp_server, "home_search_events", {"tags": ["person_present"]}))[
        "count"
    ] == 1
    assert (await call(mcp_server, "home_search_events", {"tags": ["grid_down"]}))["count"] == 0


async def test_search_rejects_an_offsetless_time(mcp_server) -> None:
    """Guessing a timezone would silently skew every stored comparison."""
    with pytest.raises(Exception) as excinfo:
        await mcp_server.call_tool("home_search_events", {"start_time": "2026-09-08T12:00:00"})
    assert "ISO 8601" in str(excinfo.value) or "ISO 8601" in str(excinfo.value.__cause__)


async def test_search_rejects_a_reversed_range(mcp_server) -> None:
    with pytest.raises(Exception) as excinfo:
        await mcp_server.call_tool(
            "home_search_events",
            {"start_time": "2026-09-08T12:00:00Z", "end_time": "2026-09-08T11:00:00Z"},
        )
    assert "must not be after" in str(excinfo.value) or "must not be after" in str(
        excinfo.value.__cause__
    )


async def test_list_zones_reports_coverage(mcp_server) -> None:
    zones = {z["key"]: z for z in (await call(mcp_server, "home_list_zones"))["zones"]}

    assert zones["front_entry"]["observed_by_cameras"] == ["front_door"]
    assert zones["backyard"]["observed_by_cameras"] == []
    assert "front_porch" in zones["front_walkway"]["adjacent_to"]


async def test_describe_home_flags_unobserved_zones(mcp_server) -> None:
    """Silence from an unwatched zone is not evidence of quiet."""
    home = await call(mcp_server, "home_describe_home")

    assert home["timezone"]
    assert {c["key"] for c in home["cameras"]} == {"front_door"}
    assert "backyard" in home["unobserved_zones"]
    assert "front_entry" not in home["unobserved_zones"]


async def test_summarize_activity_is_deterministic_counts_only(mcp_server, stored_event) -> None:
    out = await call(mcp_server, "home_summarize_activity", {})

    assert out["event_count"] == 1
    assert out["by_type"] == {"camera.person_detected": 1}
    assert out["by_zone"] == {"front_entry": 1}
    assert out["by_camera"] == {"front_door": 1}
    assert out["incident_count"] == 1
    # Structured only: no prose field anywhere for a model to have written.
    assert "narrative" not in out
    assert "summary" not in out


async def test_summarize_timeline_is_chronological(
    mcp_server, session_factory, stored_event
) -> None:
    base = now_utc()
    async with session_scope(session_factory) as session:
        for offset in (30, 60):
            session.add(
                Event(
                    uid=new_uid(),
                    delivery_key=new_uid(),
                    event_type="camera.motion",
                    source="home_assistant",
                    source_entity_id="image.front_door_event_image",
                    zone_id=None,
                    occurred_at=base - timedelta(minutes=offset),
                    received_at=base - timedelta(minutes=offset),
                    payload={},
                    payload_schema_version=1,
                    created_at=base,
                )
            )

    out = await call(mcp_server, "home_summarize_activity", {})
    times = [e["occurred_at"] for e in out["timeline"]]
    assert times == sorted(times), "a timeline should read forwards in time"


async def test_limits_are_capped(mcp_server) -> None:
    """Tool output lands in a model's context; an unbounded query is a hazard."""
    from hermes_home.services.event_service import MAX_LIMIT

    schema = next(
        t.input_schema for t in await mcp_server.list_tools() if t.name == "home_search_events"
    )
    assert schema["properties"]["limit"]["maximum"] == MAX_LIMIT


# --------------------------------------------------------------------------- #
# Routing: making the right tool the obvious one
#
# A fresh Hermes session initially answered "what happened at the front door?"
# by exploring Computer Use, Docker and Home Assistant before reaching this
# server. The tools were natively available; the problem was that the default
# 60-minute window returned zero events for the archetypal question, and a bare
# "0 events" reads as "this tool has nothing for you".
# --------------------------------------------------------------------------- #


async def test_recent_events_default_window_covers_a_day(mcp_server, stored_event) -> None:
    """The archetypal question carries no time bound; an hour is too narrow."""
    out = await call(mcp_server, "home_recent_events")

    assert out["window_minutes"] == 1440
    assert out["count"] == 1, "the default window must find today's events"


async def test_empty_window_points_at_the_data_that_exists(
    mcp_server, session_factory, stored_event
) -> None:
    """An empty result must be a signpost, not a dead end."""
    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        event.occurred_at = now_utc() - timedelta(hours=6)

    out = await call(mcp_server, "home_recent_events", {"minutes": 60})

    assert out["count"] == 0
    assert out["latest_event_at"] is not None, "must say when data does exist"
    assert "hint" in out
    assert "minutes=" in out["hint"], "the hint must name the retry that works"


async def test_hint_retry_value_actually_reaches_the_event(
    mcp_server, session_factory, stored_event
) -> None:
    """Following the hint must work, or it is worse than no hint at all."""
    import re

    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        event.occurred_at = now_utc() - timedelta(hours=6)

    empty = await call(mcp_server, "home_recent_events", {"minutes": 60})
    suggested = int(re.search(r"minutes=(\d+)", empty["hint"]).group(1))

    retried = await call(mcp_server, "home_recent_events", {"minutes": suggested})
    assert retried["count"] == 1


async def test_no_data_at_all_says_so_and_points_to_coverage(mcp_server) -> None:
    out = await call(mcp_server, "home_recent_events", {"minutes": 60})

    assert out["count"] == 0
    assert out["latest_event_at"] is None
    assert "unobserved_zones" in out["hint"], (
        "with no data, the useful next question is whether anything watches there"
    )


async def test_latest_event_at_respects_the_camera_filter(
    mcp_server, session_factory, stored_event
) -> None:
    """The signpost must describe the filtered set, not the whole database."""
    async with session_scope(session_factory) as session:
        event = await session.scalar(select(Event))
        event.occurred_at = now_utc() - timedelta(hours=6)

    matching = await call(mcp_server, "home_recent_events", {"minutes": 60, "camera": "front_door"})
    assert matching["latest_event_at"] is not None

    other = await call(mcp_server, "home_recent_events", {"minutes": 60, "camera": "nonexistent"})
    assert other["latest_event_at"] is None


async def test_descriptions_name_the_questions_they_answer(mcp_server) -> None:
    """The description is the only routing signal the model gets."""
    tools = {t.name: t.description for t in await mcp_server.list_tools()}

    recent = tools["home_recent_events"]
    assert "What happened at the front door?" in recent
    assert "package" in recent.lower()
    # It must also say what it is NOT for, or it competes with the HA tools.
    assert "Home Assistant tools" in recent
    assert "current" in recent.lower()

    assert "Summarize" in tools["home_summarize_activity"]
    assert "unobserved" in tools["home_describe_home"].lower()


async def test_descriptions_discourage_the_observed_detour(mcp_server) -> None:
    """The failure was exploration via containers/desktop; say not to."""
    recent = next(
        t.description for t in await mcp_server.list_tools() if t.name == "home_recent_events"
    )
    lowered = recent.lower()
    assert "container" in lowered
    assert "desktop" in lowered


def test_server_instructions_state_the_routing_boundary() -> None:
    from hermes_home.mcp.server import INSTRUCTIONS

    # History belongs here...
    assert "What happened at the front door?" in INSTRUCTIONS
    # ...current state and control do not.
    assert "Home Assistant tools" in INSTRUCTIONS
    assert "turn on the porch light" in INSTRUCTIONS.lower()
    # And the provenance warning the user valued must survive.
    assert "mock" in INSTRUCTIONS.lower()
