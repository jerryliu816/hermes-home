"""Did Home Assistant's events actually reach us?

Camera health cannot answer this, and the difference is not academic. On
2026-09-09 four triggers fired between 07:30 and 07:36 while every camera
reported healthy and hermes-home was polling Home Assistant successfully every
sixty seconds. The automations ran. All four POSTs vanished in transit. Nothing
in the system noticed, and the stored history simply had a hole in it that
looked exactly like a quiet morning.

So this is a second, independent axis:

    camera health    was the camera working?
    this module      did its events reach us?

**The signal is the automation's own trigger entity, never the event image.**
Measured on real hardware: ``image.garage_right_event_image`` advanced at
09:03:39 with no motion, no person detection, and no automation run. An image
entity refreshes for reasons that are not events, so keying on it would invent
delivery gaps out of nothing. The trigger entity is what the automation fires
on, so it is exactly the set of moments a delivery was owed.

Detection only. No image is fetched, and no event is ever fabricated from one --
see ``docs/event-pipeline.md`` for why backfill is unsafe here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CamerasConfig, Settings
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.time import ensure_utc, now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.repositories import DeliveryGapRepository

logger = structlog.get_logger(__name__)

#: A trigger entity is "firing" when it reads on. Anything else is idle.
_ON = "on"


@dataclass(frozen=True)
class ReconcileOutcome:
    """What one pass found for one camera."""

    camera_key: str
    triggers: int = 0
    matched: int = 0
    missing: int = 0
    new_gaps: int = 0
    resolved: int = 0
    checked_through: datetime | None = None
    error: str | None = None


def rising_edges(history: list[tuple[datetime, str]]) -> list[datetime]:
    """Instants a trigger entity transitioned into ``on``.

    Only the rising edge counts. A detection holds the sensor on for several
    seconds and Home Assistant records more than one sample, but the automation
    fires once, so one delivery is owed. Counting every ``on`` sample would
    manufacture gaps for deliveries nobody ever promised.
    """
    edges: list[datetime] = []
    previous: str | None = None
    for changed_at, state in history:
        normalized = (state or "").strip().casefold()
        if normalized == _ON and previous != _ON:
            edges.append(ensure_utc(changed_at))
        previous = normalized
    return edges


class DeliveryReconciler:
    """Compares Home Assistant's triggers against the deliveries we received."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        settings: Settings,
        cameras: CamerasConfig,
        ha_client: HomeAssistantClient,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._cameras = cameras
        self._ha = ha_client
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._settings.delivery_reconciliation_enabled:
            logger.info("reconcile.disabled")
            return
        self._task = asyncio.create_task(self._run(), name="delivery-reconcile")
        logger.info(
            "reconcile.started",
            interval_seconds=self._settings.delivery_reconciliation_interval_seconds,
            cameras=sorted(k for k, c in self._cameras.cameras.items() if c.reconcilable()),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Same rule as the health monitor: never take down the service.
                logger.exception("reconcile.pass_failed")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._settings.delivery_reconciliation_interval_seconds,
                )
            except TimeoutError:
                continue

    # ----------------------------------------------------------------- #

    async def reconcile_once(self) -> list[ReconcileOutcome]:
        """One pass over every reconcilable camera."""
        outcomes: list[ReconcileOutcome] = []
        for key, camera in self._cameras.cameras.items():
            if not camera.reconcilable():
                continue
            try:
                outcomes.append(await self._reconcile_camera(key, camera.trigger_entity or ""))
            except HomeAssistantError as exc:
                # HA unreachable means we cannot check, which is not the same as
                # finding nothing wrong. The watermark is deliberately NOT
                # advanced, so the interval stays unknown rather than clean.
                logger.warning("reconcile.unavailable", camera=key, error_code=exc.code)
                outcomes.append(ReconcileOutcome(camera_key=key, error=exc.code))
        return outcomes

    async def _reconcile_camera(self, camera_key: str, trigger_entity: str) -> ReconcileOutcome:
        moment = now_utc()
        async with session_scope(self._session_factory) as session:
            known = await DeliveryGapRepository(session).get_state(camera_key)
            boundary = ensure_utc(known.first_checked_at) if known else None
        # Triggers newer than the settle window are still legitimately in
        # flight: a delivery waits out the freshness gate and vision before it
        # is finished. Declaring those missing would report every recent event
        # as lost.
        horizon = moment - timedelta(seconds=self._settings.delivery_settle_seconds)
        window_start = moment - timedelta(
            seconds=self._settings.delivery_reconciliation_lookback_seconds
        )

        history = await self._ha.get_state_history(trigger_entity, start=window_start, end=moment)
        edges = [t for t in rising_edges(history) if t <= horizon]

        # Never reach back before reconciliation started watching this camera.
        #
        # A trigger entity's history exists whether or not an automation was
        # listening to it, so a trigger that predates the automation was never
        # owed a delivery. Reaching back produced six confident and entirely
        # false accusations of lost events: the garage automations were created
        # at 2026-09-08 23:30 and 2026-09-09 00:33, and every "loss" before
        # those instants was simply a camera firing at nobody.
        #
        # Same rule as camera health, for the same reason: we can only claim
        # knowledge from when we began observing. Earlier is unknown -- never
        # clean, and never faulty.
        if boundary is None:
            edges = []  # first pass establishes the boundary and accuses nobody
        else:
            edges = [t for t in edges if t >= boundary]

        slack = timedelta(seconds=self._settings.delivery_match_window_seconds)
        matched = missing = new_gaps = resolved = 0
        last_verified: datetime | None = None

        async with session_scope(self._session_factory) as session:
            repo = DeliveryGapRepository(session)
            deliveries = await repo.deliveries_between(
                camera_key=camera_key,
                start=window_start - slack,
                end=moment + slack,
            )
            received = [(d.received_at, d.id) for d in deliveries]

            for edge in edges:
                hit = next(
                    ((ts, did) for ts, did in received if abs(ts - edge) <= slack),
                    None,
                )
                if hit is not None:
                    matched += 1
                    last_verified = edge if last_verified is None else max(last_verified, edge)
                    # A gap recorded earlier can be resolved by a late arrival.
                    if await repo.resolve_gap(
                        camera_key=camera_key, ha_trigger_at=edge, delivery_id=hit[1]
                    ):
                        resolved += 1
                        logger.info(
                            "delivery.gap_resolved",
                            camera=camera_key,
                            ha_trigger_at=edge.isoformat(),
                        )
                    continue

                missing += 1
                gap = await repo.record_gap(
                    camera_key=camera_key,
                    trigger_entity=trigger_entity,
                    ha_trigger_at=edge,
                    ha_image_ts=None,
                    detected_at=moment,
                )
                if gap is not None:
                    new_gaps += 1
                    logger.warning(
                        "delivery.gap_detected",
                        camera=camera_key,
                        trigger_entity=trigger_entity,
                        ha_trigger_at=edge.isoformat(),
                        reason=gap.reason,
                    )

            await repo.mark_checked(
                camera_key=camera_key,
                checked_at=moment,
                # The first pass establishes the boundary at the moment it ran,
                # not at the start of its lookback: we cannot know whether an
                # automation existed before we were watching.
                checked_from=moment,
                checked_through=horizon,
                last_verified_delivery_at=last_verified,
            )

        return ReconcileOutcome(
            camera_key=camera_key,
            triggers=len(edges),
            matched=matched,
            missing=missing,
            new_gaps=new_gaps,
            resolved=resolved,
            checked_through=horizon,
        )
