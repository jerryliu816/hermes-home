"""The background worker.

An asyncio task in the same process as the API. That is the whole design: for a
single home this is a handful of events a day, and a broker would add an
operational dependency without buying anything.

Durability comes from SQLite, not from the loop. A delivery is committed before
the webhook answers, claiming is a single atomic UPDATE, and a worker killed
mid-job leaves a lease that expires and makes the delivery claimable again.
Restarting the process is a complete recovery mechanism.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CamerasConfig, Settings
from hermes_home.domain.event_types import UnknownEventTypeError
from hermes_home.ingest.correlate import close_stale_incidents
from hermes_home.ingest.pipeline import ProcessingError, process_delivery
from hermes_home.ingest.retention import prune_raw_bodies
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import Disposition
from hermes_home.storage.repositories import DeliveryRepository
from hermes_home.vision.base import VisionProvider

logger = structlog.get_logger(__name__)


class IngestWorker:
    """Claims deliveries and runs them through the pipeline."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        settings: Settings,
        cameras: CamerasConfig,
        ha_client: HomeAssistantClient,
        vision: VisionProvider,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._cameras = cameras
        self._ha_client = ha_client
        self._vision = vision

        # Lets the webhook wake the worker immediately instead of waiting out
        # the poll interval. Purely a latency optimization -- correctness comes
        # from the database, so a missed nudge only means a slower pickup.
        self._wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    def notify(self) -> None:
        self._wakeup.set()

    async def start(self) -> None:
        for index in range(self._settings.ingest_worker_concurrency):
            self._tasks.append(asyncio.create_task(self._run(index), name=f"ingest-worker-{index}"))
        self._tasks.append(asyncio.create_task(self._run_maintenance(), name="maintenance"))
        logger.info("worker.started", concurrency=self._settings.ingest_worker_concurrency)

    async def stop(self, *, grace_seconds: float | None = None) -> None:
        """Stop claiming work, let anything in flight finish, then exit.

        Correctness does not depend on this: a delivery abandoned mid-job keeps
        its lease, which expires and makes it claimable again. But finishing the
        current job avoids a needless retry and avoids paying a second time for
        a vision call that already succeeded. A job that overruns the grace
        period is cancelled and recovered the ordinary way.
        """
        grace = (
            grace_seconds if grace_seconds is not None else self._settings.shutdown_grace_seconds
        )
        self._stopping.set()
        self._wakeup.set()

        pending = [t for t in self._tasks if not t.done()]
        if pending:
            _done, still_running = await asyncio.wait(pending, timeout=grace)
            if still_running:
                logger.warning(
                    "worker.shutdown_timeout",
                    grace_seconds=grace,
                    unfinished=len(still_running),
                    note="in-flight work stays recoverable via lease expiry",
                )
                for task in still_running:
                    task.cancel()
                await asyncio.gather(*still_running, return_exceptions=True)

        self._tasks.clear()
        logger.info("worker.stopped")

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    # ----------------------------------------------------------------- #

    async def _run(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                processed = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker.loop_error", worker=index)
                processed = False

            if not processed:
                self._wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(), timeout=self._settings.ingest_poll_seconds
                    )
                except TimeoutError:
                    pass

    async def drain_once(self) -> bool:
        """Process one delivery if any is available. Returns whether it did."""
        async with session_scope(self._session_factory) as session:
            deliveries = DeliveryRepository(session)
            delivery = await deliveries.claim_next(
                lease_seconds=self._settings.ingest_lease_seconds
            )
            if delivery is None:
                return False

            log = logger.bind(correlation_id=delivery.correlation_id, delivery_uid=delivery.uid)
            log.info("worker.claimed", attempt=delivery.attempts)

            try:
                outcome = await process_delivery(
                    session,
                    delivery,
                    settings=self._settings,
                    cameras=self._cameras,
                    ha_client=self._ha_client,
                    vision=self._vision,
                )
            except ProcessingError as exc:
                will_retry = await deliveries.mark_retry_or_fail(
                    delivery,
                    error_code=exc.code,
                    error_message=str(exc),
                    max_attempts=self._settings.ingest_max_attempts,
                )
                log.warning(
                    "worker.transient_failure",
                    error_code=exc.code,
                    will_retry=will_retry,
                    attempts=delivery.attempts,
                )
                return True
            except (UnknownEventTypeError, KeyError, ValueError) as exc:
                # Malformed or unsupported: retrying cannot help.
                await deliveries.mark_rejected(
                    delivery,
                    disposition=Disposition.REJECTED_INVALID,
                    note=f"{type(exc).__name__}: {exc}"[:500],
                )
                log.warning("worker.rejected", reason=str(exc)[:200])
                return True
            except Exception as exc:
                will_retry = await deliveries.mark_retry_or_fail(
                    delivery,
                    error_code="unexpected",
                    error_message=f"{type(exc).__name__}: {exc}",
                    max_attempts=self._settings.ingest_max_attempts,
                )
                log.exception("worker.unexpected_failure", will_retry=will_retry)
                return True

            if outcome.disposition in (
                Disposition.REJECTED_INVALID,
                Disposition.REJECTED_STALE_IMAGE,
            ):
                await deliveries.mark_rejected(
                    delivery, disposition=outcome.disposition, note=outcome.note
                )
            else:
                event_id = None
                if outcome.event_uid:
                    from hermes_home.storage.repositories import EventRepository

                    event = await EventRepository(session).get_by_uid(outcome.event_uid)
                    event_id = event.id if event else None
                await deliveries.mark_completed(
                    delivery,
                    disposition=outcome.disposition,
                    event_id=event_id,
                    note=outcome.note,
                )
            log.info("worker.finished", disposition=outcome.disposition)
            return True

    async def _run_maintenance(self) -> None:
        """Periodic housekeeping: close settled incidents, prune old payloads.

        One loop rather than one task per job. It ticks at the shorter of the
        two cadences (incidents, ~60s) and runs retention on its own much longer
        schedule, so adding a third chore later costs a branch rather than
        another scheduler.
        """
        last_retention = 0.0
        while not self._stopping.is_set():
            try:
                await self.close_settled_incidents()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("incidents.sweep_failed")

            elapsed = asyncio.get_running_loop().time() - last_retention
            if last_retention == 0.0 or elapsed >= self._settings.retention_sweep_interval_seconds:
                try:
                    await prune_raw_bodies(
                        self._session_factory,
                        retention_days=self._settings.delivery_raw_retention_days,
                    )
                    last_retention = asyncio.get_running_loop().time()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("retention.sweep_failed")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._settings.incident_sweep_interval_seconds,
                )
            except TimeoutError:
                continue

    async def close_settled_incidents(self) -> int:
        """Close incidents that can no longer receive a correlated event."""
        async with session_scope(self._session_factory) as session:
            return await close_stale_incidents(
                session, idle_seconds=self._settings.incident_idle_seconds
            )
