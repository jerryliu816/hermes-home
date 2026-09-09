"""Incident lifecycle: staying open only while another event could still join.

Found in production: nothing ever closed an incident, so every one sat `open`
indefinitely — harmless at the time, because correlation is time-windowed, but
it made the status field meaningless and would have broken any query that
trusted it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc
from hermes_home.ingest.correlate import (
    build_incident_summary,
    close_stale_incidents,
    correlate,
)
from hermes_home.ingest.worker import IngestWorker
from hermes_home.spatial import zone_by_key
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Event, Incident
from hermes_home.storage.repositories import EventRepository, IncidentRepository
from hermes_home.vision.mock import MockVisionProvider


async def _add_event(session, *, occurred_at, zone_key="front_entry", tags=()):
    zone = await zone_by_key(session, zone_key)
    event = Event(
        uid=new_uid(),
        delivery_key=new_uid(),
        event_type="camera.person_detected",
        source="home_assistant",
        source_entity_id="image.front_door_event_image",
        zone_id=zone.id if zone else None,
        occurred_at=occurred_at,
        received_at=occurred_at,
        payload={},
        payload_schema_version=1,
        created_at=now_utc(),
    )
    repo = EventRepository(session)
    await repo.create(event)
    if tags:
        await repo.add_tags(event.id, list(tags), source="vision")
    await correlate(IncidentRepository(session), event, window_seconds=120, occurred_at=occurred_at)
    return event


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def test_incident_closes_once_idle(session_factory) -> None:
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc() - timedelta(minutes=10))

    async with session_scope(session_factory) as session:
        assert await close_stale_incidents(session, idle_seconds=120) == 1

    async with session_scope(session_factory) as session:
        incident = await session.scalar(select(Incident))
        assert incident.status == "closed"
        assert incident.summary


async def test_active_incident_stays_open(session_factory) -> None:
    """Still inside the window: another event could legitimately join it."""
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc() - timedelta(seconds=5))

    async with session_scope(session_factory) as session:
        assert await close_stale_incidents(session, idle_seconds=120) == 0

    async with session_scope(session_factory) as session:
        assert (await session.scalar(select(Incident))).status == "open"
        assert (await session.scalar(select(Incident))).summary is None


async def test_event_inside_the_window_extends_the_same_incident(session_factory) -> None:
    base = now_utc() - timedelta(seconds=90)
    async with session_scope(session_factory) as session:
        first = await _add_event(session, occurred_at=base)
        second = await _add_event(session, occurred_at=base + timedelta(seconds=30))

    async with session_scope(session_factory) as session:
        assert await session.scalar(select(func.count()).select_from(Incident)) == 1
        incident = await session.scalar(select(Incident))
        assert incident.ended_at > incident.started_at, "the span must have grown"
        events = list((await session.scalars(select(Event))).all())
        assert {e.incident_id for e in events} == {incident.id}
    assert first.uid != second.uid


async def test_event_after_close_starts_a_new_incident(session_factory) -> None:
    """A closed incident is settled and must never be revived."""
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc() - timedelta(minutes=10))
    async with session_scope(session_factory) as session:
        await close_stale_incidents(session, idle_seconds=120)

    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc())

    async with session_scope(session_factory) as session:
        incidents = list((await session.scalars(select(Incident).order_by(Incident.id))).all())
        assert len(incidents) == 2
        assert incidents[0].status == "closed"
        assert incidents[1].status == "open"
        # The closed one keeps exactly the events it had.
        assert incidents[0].summary.startswith("1 event")


async def test_sweep_is_idempotent(session_factory) -> None:
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc() - timedelta(minutes=10))

    async with session_scope(session_factory) as session:
        first_pass = await close_stale_incidents(session, idle_seconds=120)
    async with session_scope(session_factory) as session:
        summary_after_first = (await session.scalar(select(Incident))).summary
        second_pass = await close_stale_incidents(session, idle_seconds=120)
    async with session_scope(session_factory) as session:
        summary_after_second = (await session.scalar(select(Incident))).summary

    assert (first_pass, second_pass) == (1, 0)
    assert summary_after_first == summary_after_second


async def test_backfill_closes_pre_existing_stale_incidents(session_factory) -> None:
    """The condition production was actually in: several long-open incidents."""
    base = now_utc() - timedelta(hours=14)
    async with session_scope(session_factory) as session:
        for offset in (0, 1, 2, 3, 4):
            await _add_event(session, occurred_at=base + timedelta(hours=offset))

    async with session_scope(session_factory) as session:
        open_before = await session.scalar(
            select(func.count()).select_from(Incident).where(Incident.status == "open")
        )
        assert open_before == 5

    async with session_scope(session_factory) as session:
        closed = await close_stale_incidents(session, idle_seconds=120)

    async with session_scope(session_factory) as session:
        assert closed == 5
        assert (
            await session.scalar(
                select(func.count()).select_from(Incident).where(Incident.status == "open")
            )
            == 0
        )
        assert all(i.summary for i in (await session.scalars(select(Incident))).all())


async def test_closing_never_alters_event_membership(session_factory) -> None:
    """Closing is a status change, not a re-correlation."""
    base = now_utc() - timedelta(minutes=10)
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=base)
        await _add_event(session, occurred_at=base + timedelta(seconds=20))

    async with session_scope(session_factory) as session:
        before = {e.uid: e.incident_id for e in (await session.scalars(select(Event))).all()}

    async with session_scope(session_factory) as session:
        await close_stale_incidents(session, idle_seconds=120)

    async with session_scope(session_factory) as session:
        after = {e.uid: e.incident_id for e in (await session.scalars(select(Event))).all()}
        assert before == after


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


def test_summary_is_deterministic() -> None:
    args = dict(
        event_count=2,
        zone_key="front_entry",
        duration_seconds=35,
        tags=["person_present", "package_present"],
    )
    assert build_incident_summary(**args) == build_incident_summary(**args)


def test_summary_tag_order_does_not_matter() -> None:
    """Tag ordering comes out of a set elsewhere; the summary must still be stable."""
    a = build_incident_summary(event_count=1, zone_key="z", duration_seconds=0, tags=["b", "a"])
    b = build_incident_summary(event_count=1, zone_key="z", duration_seconds=0, tags=["a", "b"])
    assert a == b == "1 event in z; a, b"


def test_summary_shape_matches_the_agreed_format() -> None:
    assert (
        build_incident_summary(
            event_count=2,
            zone_key="front_entry",
            duration_seconds=35,
            tags=["person_present", "package_present"],
        )
        == "2 events in front_entry over 35s; package_present, person_present"
    )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, None), (35, "35s"), (60, "1m"), (185, "3m 5s"), (3600, "1h"), (3900, "1h 5m")],
)
def test_duration_formatting(seconds: int, expected: str | None) -> None:
    summary = build_incident_summary(event_count=1, zone_key="z", duration_seconds=seconds, tags=[])
    if expected is None:
        assert "over" not in summary, "an instantaneous event has no span to report"
    else:
        assert summary == f"1 event in z over {expected}"


def test_summary_omits_the_tag_clause_when_there_are_none() -> None:
    assert (
        build_incident_summary(event_count=3, zone_key="driveway", duration_seconds=10, tags=[])
        == "3 events in driveway over 10s"
    )


async def test_summary_reflects_real_events_and_deduplicates_tags(session_factory) -> None:
    base = now_utc() - timedelta(minutes=10)
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=base, tags=["person_present"])
        await _add_event(
            session,
            occurred_at=base + timedelta(seconds=35),
            tags=["person_present", "package_present"],
        )

    async with session_scope(session_factory) as session:
        await close_stale_incidents(session, idle_seconds=120)

    async with session_scope(session_factory) as session:
        incident = await session.scalar(select(Incident))

    assert incident.summary == ("2 events in front_entry over 35s; package_present, person_present")


async def test_summary_generation_makes_no_provider_call(session_factory, monkeypatch) -> None:
    """Summaries are a record, not a narrative. No model is consulted."""
    calls: list[str] = []

    class ExplodingVision(MockVisionProvider):
        async def analyze(self, request):  # type: ignore[override]
            calls.append("analyze")
            raise AssertionError("summary generation must not invoke a vision provider")

    async def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        calls.append("network")
        raise AssertionError("summary generation must not make a network call")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)

    async with session_scope(session_factory) as session:
        await _add_event(
            session, occurred_at=now_utc() - timedelta(minutes=10), tags=["person_present"]
        )
    async with session_scope(session_factory) as session:
        assert await close_stale_incidents(session, idle_seconds=120) == 1

    assert calls == []


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


async def test_worker_closes_incidents_through_maintenance(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """The sweep must actually be reachable from the running worker."""
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc() - timedelta(minutes=10))

    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
    )
    assert await worker.close_settled_incidents() == 1

    async with session_scope(session_factory) as session:
        assert (await session.scalar(select(Incident))).status == "closed"


async def test_idle_threshold_is_configurable(session_factory, settings) -> None:
    async with session_scope(session_factory) as session:
        await _add_event(session, occurred_at=now_utc() - timedelta(seconds=30))

    # Default 120s leaves it open...
    async with session_scope(session_factory) as session:
        assert await close_stale_incidents(session, idle_seconds=120) == 0
    # ...a shorter threshold closes it.
    async with session_scope(session_factory) as session:
        assert await close_stale_incidents(session, idle_seconds=10) == 1
