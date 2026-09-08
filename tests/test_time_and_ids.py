"""Datetime discipline and key derivation -- risks #1 and #2 in the design."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from hermes_home.core.ids import content_hash, delivery_key
from hermes_home.core.time import (
    NaiveDatetimeError,
    ensure_utc,
    now_utc,
    parse_ha_timestamp,
    to_display_tz,
)
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import EventDelivery


def test_now_utc_is_aware() -> None:
    assert now_utc().tzinfo is not None


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001 - naive is the point


def test_ensure_utc_normalizes_offset() -> None:
    pacific = datetime(2026, 1, 1, 4, 0, 0, tzinfo=timezone(-timedelta(hours=8)))
    assert ensure_utc(pacific) == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw",
    ["2026-09-08T12:00:00Z", "2026-09-08T12:00:00+00:00", "2026-09-08T05:00:00-07:00"],
)
def test_parse_ha_timestamp_accepts_offsets(raw: str) -> None:
    assert parse_ha_timestamp(raw).tzinfo is not None


def test_parse_ha_timestamp_refuses_offsetless() -> None:
    # Assuming UTC here would silently skew every downstream time window.
    with pytest.raises(NaiveDatetimeError):
        parse_ha_timestamp("2026-09-08T12:00:00")


def test_to_display_tz_converts_without_changing_instant() -> None:
    moment = datetime(2026, 9, 8, 20, 0, 0, tzinfo=UTC)
    local = to_display_tz(moment, "America/Los_Angeles")
    assert local.hour == 13
    assert local.astimezone(UTC) == moment


async def test_database_refuses_naive_datetime(session_factory) -> None:
    """The type decorator, not just the helper, must refuse naive input.

    SQLAlchemy wraps bind-parameter errors in StatementError, so assert on the
    underlying cause -- what matters is that the write cannot land.
    """
    from sqlalchemy.exc import StatementError

    with pytest.raises((NaiveDatetimeError, StatementError)) as excinfo:
        async with session_scope(session_factory) as session:
            session.add(
                EventDelivery(
                    uid="x",
                    received_at=datetime(2026, 1, 1, 0, 0, 0),  # noqa: DTZ001 - naive on purpose
                    source="home_assistant",
                    delivery_key="k",
                    correlation_id="c",
                    next_attempt_at=now_utc(),
                    raw_body={},
                )
            )
            await session.flush()

    root = excinfo.value
    while root.__cause__ is not None:
        root = root.__cause__
    assert isinstance(root, NaiveDatetimeError)


async def test_datetime_round_trips_as_utc(session_factory) -> None:
    moment = datetime(2026, 9, 8, 20, 30, 15, 123456, tzinfo=UTC)
    async with session_scope(session_factory) as session:
        session.add(
            EventDelivery(
                uid="round-trip",
                received_at=moment,
                source="home_assistant",
                delivery_key="k1",
                correlation_id="c1",
                next_attempt_at=moment,
                raw_body={},
            )
        )
    async with session_scope(session_factory) as session:
        stored = await session.scalar(
            select(EventDelivery).where(EventDelivery.uid == "round-trip")
        )
        assert stored is not None
        assert stored.received_at == moment
        assert stored.received_at.tzinfo is not None


def test_delivery_key_is_deterministic() -> None:
    args = {
        "source": "home_assistant",
        "event_type": "camera.person_detected",
        "source_entity_id": "image.front_door_event_image",
        "occurred_at": datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC),
    }
    assert delivery_key(**args) == delivery_key(**args)


def test_delivery_key_separates_events_one_second_apart() -> None:
    base = datetime(2026, 9, 8, 12, 0, 0, tzinfo=UTC)
    common = {
        "source": "home_assistant",
        "event_type": "camera.person_detected",
        "source_entity_id": "image.front_door",
    }
    assert delivery_key(**common, occurred_at=base) != delivery_key(
        **common, occurred_at=base + timedelta(seconds=1)
    )


def test_delivery_key_ignores_sub_millisecond_jitter() -> None:
    """Millisecond granularity: the same physical event redelivered must match."""
    base = datetime(2026, 9, 8, 12, 0, 0, 500_000, tzinfo=UTC)
    jittered = base.replace(microsecond=500_400)
    common = {
        "source": "home_assistant",
        "event_type": "camera.person_detected",
        "source_entity_id": "image.front_door",
    }
    assert delivery_key(**common, occurred_at=base) == delivery_key(**common, occurred_at=jittered)


def test_content_hash_distinguishes_bytes() -> None:
    assert content_hash(b"a") != content_hash(b"b")
    assert content_hash(b"a") == content_hash(b"a")
