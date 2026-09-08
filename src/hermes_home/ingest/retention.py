"""Bounded retention for raw webhook bodies.

We keep inbound deliveries because ingestion problems are otherwise impossible to
diagnose after the fact. We do not keep the payloads forever: this service
watches a private home, and a body can name entities and describe activity.

What goes: ``raw_body``, and only that.
What stays, permanently: when it arrived, its status and disposition, how many
attempts it took, any error, and which event it became. That is enough to answer
"what happened to this delivery" long after the payload is gone.

Headers are never stored at all -- they carry the shared secret.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermes_home.storage.engine import session_scope
from hermes_home.storage.repositories import DeliveryRepository

logger = structlog.get_logger(__name__)


async def prune_raw_bodies(session_factory: async_sessionmaker, *, retention_days: int) -> int:
    async with session_scope(session_factory) as session:
        pruned = await DeliveryRepository(session).prune_raw_bodies(older_than_days=retention_days)
    if pruned:
        logger.info("retention.pruned_raw_bodies", count=pruned, retention_days=retention_days)
    return pruned
