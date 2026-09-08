"""Data access. Everything that touches the database goes through here.

The interesting piece is :meth:`DeliveryRepository.claim_next`. Claiming a job is
a single atomic UPDATE, which buys two things at once: two workers cannot take
the same delivery, and a delivery abandoned by a crashed worker becomes eligible
again the moment its lease expires -- crash recovery falls out of the same query
that does normal dispatch, with no separate reaper to write or forget.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_home.core.ids import new_uid
from hermes_home.core.time import ensure_utc, now_utc
from hermes_home.storage.models import (
    DeliveryStatus,
    Event,
    EventAnalysis,
    EventDelivery,
    EventTag,
    Incident,
)


class DeliveryRepository:
    """The inbound audit log, which is also the durable work queue."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        *,
        source: str,
        delivery_key: str,
        correlation_id: str,
        raw_body: dict[str, Any],
        received_at: datetime | None = None,
    ) -> EventDelivery:
        """Persist an accepted webhook. Committed before the HTTP 202 goes out."""
        moment = ensure_utc(received_at or now_utc())
        delivery = EventDelivery(
            uid=new_uid(),
            received_at=moment,
            source=source,
            delivery_key=delivery_key,
            correlation_id=correlation_id,
            status=DeliveryStatus.PENDING,
            attempts=0,
            next_attempt_at=moment,
            raw_body=raw_body,
        )
        self._session.add(delivery)
        await self._session.flush()
        return delivery

    async def claim_next(self, *, lease_seconds: int) -> EventDelivery | None:
        """Atomically take the oldest runnable delivery, or return None.

        Runnable means either genuinely pending and due, or stuck in
        ``processing`` past its lease -- which is precisely the state a worker
        killed mid-job leaves behind.
        """
        moment = now_utc()
        lease_until = moment + timedelta(seconds=lease_seconds)

        candidate = (
            select(EventDelivery.id)
            .where(
                or_(
                    (EventDelivery.status == DeliveryStatus.PENDING)
                    & (EventDelivery.next_attempt_at <= moment),
                    (EventDelivery.status == DeliveryStatus.PROCESSING)
                    & (EventDelivery.lease_expires_at < moment),
                )
            )
            .order_by(EventDelivery.received_at)
            .limit(1)
            .scalar_subquery()
        )

        result = await self._session.execute(
            update(EventDelivery)
            .where(EventDelivery.id == candidate)
            .values(
                status=DeliveryStatus.PROCESSING,
                attempts=EventDelivery.attempts + 1,
                lease_expires_at=lease_until,
            )
            .returning(EventDelivery.id)
        )
        row = result.first()
        if row is None:
            return None
        await self._session.commit()
        return await self._session.get(EventDelivery, row[0])

    async def mark_completed(
        self,
        delivery: EventDelivery,
        *,
        disposition: str,
        event_id: int | None,
        note: str | None = None,
    ) -> None:
        delivery.status = DeliveryStatus.COMPLETED
        delivery.disposition = disposition
        delivery.event_id = event_id
        delivery.note = note
        delivery.lease_expires_at = None
        delivery.last_error_code = None
        delivery.last_error_message = None

    async def mark_rejected(
        self, delivery: EventDelivery, *, disposition: str, note: str | None = None
    ) -> None:
        """Terminal, but not an error: we understood it and chose not to store it."""
        delivery.status = DeliveryStatus.REJECTED
        delivery.disposition = disposition
        delivery.note = note
        delivery.lease_expires_at = None

    async def mark_retry_or_fail(
        self,
        delivery: EventDelivery,
        *,
        error_code: str,
        error_message: str,
        max_attempts: int,
        backoff_base_seconds: float = 2.0,
    ) -> bool:
        """Schedule another attempt, or give up. Returns True if it will retry."""
        delivery.last_error_code = error_code
        delivery.last_error_message = error_message[:1000]
        delivery.lease_expires_at = None

        if delivery.attempts >= max_attempts:
            delivery.status = DeliveryStatus.FAILED
            return False

        delay = backoff_base_seconds * (2 ** (delivery.attempts - 1))
        delivery.status = DeliveryStatus.PENDING
        delivery.next_attempt_at = now_utc() + timedelta(seconds=min(delay, 300.0))
        return True

    async def get_by_uid(self, uid: str) -> EventDelivery | None:
        return await self._session.scalar(select(EventDelivery).where(EventDelivery.uid == uid))

    async def count_by_status(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(EventDelivery.status, func.count()).group_by(EventDelivery.status)
        )
        return {status: count for status, count in rows}

    async def prune_raw_bodies(self, *, older_than_days: int) -> int:
        """Null out raw webhook bodies past the retention horizon.

        The delivery row itself survives permanently -- timestamps, status,
        disposition, and the link to its event are what make an ingestion
        problem diagnosable months later. Only the payload goes.
        """
        cutoff = now_utc() - timedelta(days=older_than_days)
        result = await self._session.execute(
            update(EventDelivery)
            .where(EventDelivery.received_at < cutoff, EventDelivery.raw_body.is_not(None))
            .values(raw_body=None)
        )
        return result.rowcount or 0


class EventRepository:
    """Immutable observations, their tags, and their analyses."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_delivery_key(self, delivery_key: str) -> Event | None:
        return await self._session.scalar(select(Event).where(Event.delivery_key == delivery_key))

    async def find_recent_by_content_hash(
        self, *, source_entity_id: str | None, content_hash: str, window_seconds: int
    ) -> Event | None:
        """Same bytes, same camera, moments ago: one occurrence delivered twice."""
        since = now_utc() - timedelta(seconds=window_seconds)
        return await self._session.scalar(
            select(Event)
            .where(
                Event.content_hash == content_hash,
                Event.source_entity_id == source_entity_id,
                Event.occurred_at >= since,
            )
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )

    async def latest_content_hash(self, source_entity_id: str) -> str | None:
        """The content hash of the most recent event for this entity, unbounded.

        Deliberately not time-windowed. It answers a different question from the
        dedupe window: not "did this arrive twice just now" but "is this the very
        same frame we already have", which is how a stale image is recognised
        when the entity's own timestamp is unavailable.
        """
        return await self._session.scalar(
            select(Event.content_hash)
            .where(Event.source_entity_id == source_entity_id, Event.content_hash.is_not(None))
            .order_by(Event.occurred_at.desc())
            .limit(1)
        )

    async def latest_source_state_ts(self, source_entity_id: str) -> datetime | None:
        """The freshest image timestamp we have already consumed for this entity."""
        return await self._session.scalar(
            select(func.max(Event.source_state_ts)).where(
                Event.source_entity_id == source_entity_id
            )
        )

    async def create(self, event: Event) -> Event:
        self._session.add(event)
        await self._session.flush()
        return event

    async def add_tags(self, event_id: int, tags: list[str], *, source: str) -> None:
        existing = set(
            (
                await self._session.scalars(
                    select(EventTag.tag).where(EventTag.event_id == event_id)
                )
            ).all()
        )
        for tag in tags:
            if tag not in existing:
                self._session.add(EventTag(event_id=event_id, tag=tag, source=source))
                existing.add(tag)

    async def add_analysis(self, analysis: EventAnalysis) -> EventAnalysis:
        self._session.add(analysis)
        await self._session.flush()
        return analysis

    async def increment_duplicate_count(self, event: Event) -> None:
        event.duplicate_count += 1

    async def get_by_uid(self, uid: str) -> Event | None:
        return await self._session.scalar(select(Event).where(Event.uid == uid))

    async def analyses_for(self, event_id: int) -> list[EventAnalysis]:
        return list(
            (
                await self._session.scalars(
                    select(EventAnalysis)
                    .where(EventAnalysis.event_id == event_id)
                    .order_by(EventAnalysis.attempt)
                )
            ).all()
        )

    async def tags_for(self, event_id: int) -> list[str]:
        return sorted(
            (
                await self._session.scalars(
                    select(EventTag.tag).where(EventTag.event_id == event_id)
                )
            ).all()
        )

    async def search(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        event_type_prefix: str | None = None,
        zone_id: int | None = None,
        source_entity_id: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Time-range query with the filters the MCP tools expose."""
        stmt = select(Event)
        if start is not None:
            stmt = stmt.where(Event.occurred_at >= ensure_utc(start))
        if end is not None:
            stmt = stmt.where(Event.occurred_at <= ensure_utc(end))
        if event_type_prefix:
            stmt = stmt.where(Event.event_type.like(f"{event_type_prefix}%"))
        if zone_id is not None:
            stmt = stmt.where(Event.zone_id == zone_id)
        if source_entity_id:
            stmt = stmt.where(Event.source_entity_id == source_entity_id)
        if tags:
            for tag in tags:
                stmt = stmt.where(
                    Event.id.in_(select(EventTag.event_id).where(EventTag.tag == tag))
                )
        stmt = stmt.order_by(Event.occurred_at.desc()).limit(limit)
        return list((await self._session.scalars(stmt)).all())


class IncidentRepository:
    """Groups of events that look like one real-world occurrence."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_open_in_zone(
        self, *, zone_id: int | None, window_seconds: int, at: datetime
    ) -> Incident | None:
        if zone_id is None:
            return None
        floor = ensure_utc(at) - timedelta(seconds=window_seconds)
        return await self._session.scalar(
            select(Incident)
            .where(
                Incident.zone_id == zone_id,
                Incident.status == "open",
                func.coalesce(Incident.ended_at, Incident.started_at) >= floor,
            )
            .order_by(Incident.started_at.desc())
            .limit(1)
        )

    async def create(self, incident: Incident) -> Incident:
        self._session.add(incident)
        await self._session.flush()
        return incident

    async def get_by_uid(self, uid: str) -> Incident | None:
        return await self._session.scalar(select(Incident).where(Incident.uid == uid))

    async def delete_all(self) -> None:
        """Incidents are derived and disposable; improving the rule means recomputing."""
        await self._session.execute(delete(Incident))
