"""Copies sensor history out of Home Assistant's recorder.

Its own asyncio task, like the camera health monitor and for the same reasons:
it must keep running while ingestion is idle, and a failure here must never
touch webhook acceptance or vision processing.

Pull, not push. Each pass asks HA for every state change since the point this
measurement was last synced, so no automation is needed in Home Assistant and a
restart of this service -- or a week with the machine off -- loses nothing HA
still holds. A sensor newly added to ``config/sensors.yaml`` has no sync point
yet, and is backfilled from as far back as ``SENSOR_BACKFILL_DAYS`` reaches.

What HA cannot account for is recorded as such. If its history for a span is
gone (purged, or the entity did not exist yet), a ``no_record`` row marks that
span: the alternative, extending the last value we had across it, is a reading
nobody took.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import MeasurementConfig, SensorsConfig, Settings
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import SensorState, SensorStatus, SensorSyncState
from hermes_home.storage.repositories import SensorRepository

logger = structlog.get_logger(__name__)

#: How far behind "now" each pass stops. HA's recorder commits in batches
#: (every few seconds by default), so a change made a moment ago may not be
#: queryable yet; syncing right up to now would skip past it and later record
#: it at the wrong time.
RECORDER_LAG = timedelta(seconds=30)

#: How much later than the requested start HA's first returned state may be and
#: still count as covering the start. HA stamps the state in force at the start
#: of a history window with the window's start time, so beyond a little clock
#: noise, a later first entry means HA held nothing for the span before it.
_START_SLACK = timedelta(seconds=2)

#: Retention runs inside the recorder, at most this often.
_PRUNE_EVERY = timedelta(hours=1)


def classify(raw: str) -> tuple[float | None, str]:
    """A Home Assistant state string as (value, status). Unknown is never 0."""
    text = raw.strip()
    lowered = text.lower()
    if lowered == "unavailable":
        return None, SensorStatus.UNAVAILABLE
    if lowered in ("unknown", "", "none"):
        return None, SensorStatus.UNKNOWN
    try:
        value = float(text)
    except ValueError:
        return None, SensorStatus.UNKNOWN
    if not math.isfinite(value):
        return None, SensorStatus.UNKNOWN
    return value, SensorStatus.OK


class SensorRecorder:
    """Keeps ``sensor_states`` in step with Home Assistant's recorder."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker,
        settings: Settings,
        sensors: SensorsConfig,
        ha_client: HomeAssistantClient,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._sensors = sensors
        self._ha = ha_client
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_prune: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._settings.sensor_sync_enabled:
            logger.info("sensors.disabled")
            return
        if not self._sensors.sensors:
            logger.info("sensors.none_configured")
            return
        self._task = asyncio.create_task(self._run(), name="sensor-recorder")
        logger.info(
            "sensors.started",
            interval_seconds=self._settings.sensor_sync_interval_seconds,
            sensors=len(self._sensors.sensors),
            measurements=len(self._sensors.measurement_entities()),
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
                await self.sync_once()
                await self._maybe_prune()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sensors.sync_failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.sensor_sync_interval_seconds
                )
            except TimeoutError:
                continue

    async def _maybe_prune(self) -> None:
        moment = now_utc()
        if self._last_prune is not None and moment - self._last_prune < _PRUNE_EVERY:
            return
        async with session_scope(self._session_factory) as session:
            pruned = await SensorRepository(session).prune(
                older_than_days=self._settings.sensor_retention_days
            )
        self._last_prune = moment
        if pruned:
            logger.info("sensors.pruned", count=pruned)

    # ----------------------------------------------------------------- #

    async def sync_once(self) -> dict[str, int]:
        """One pass over every configured measurement. Returns rows added per key."""
        added: dict[str, int] = {}
        for key, name, cfg in self._sensors.measurement_entities():
            added[f"{key}.{name}"] = await self._sync_measurement(key, name, cfg)
        return added

    async def _sync_measurement(self, key: str, name: str, cfg: MeasurementConfig) -> int:
        moment = now_utc()
        until = moment - RECORDER_LAG

        async with session_scope(self._session_factory) as session:
            repo = SensorRepository(session)
            sync = await repo.sync_state(key, name)
            if sync is None:
                sync = SensorSyncState(
                    sensor_key=key,
                    measurement=name,
                    entity_id=cfg.entity,
                    synced_through=until - timedelta(days=self._settings.sensor_backfill_days),
                    first_synced_at=moment,
                )
                repo.add_sync_state(sync)
            # A renamed entity continues the same history under the same key.
            sync.entity_id = cfg.entity
            sync.last_attempt_at = moment
            cursor = sync.synced_through
            if until <= cursor:
                return 0

            try:
                state = await self._ha.get_entity_state(cfg.entity)
                unit = state.attributes.get("unit_of_measurement")
                history = await self._ha.get_state_history(cfg.entity, start=cursor, end=until)
            except HomeAssistantError as exc:
                # Our blindness, not the sensor's: write nothing about the
                # sensor and leave the sync point where it is, so the next pass
                # asks for the same span again.
                sync.last_error = exc.code
                logger.warning("sensors.ha_error", sensor=key, measurement=name, code=exc.code)
                return 0

            if unit is not None:
                sync.unit = str(unit)[:32]
            added = await self._store(repo, key, name, cursor, history, sync.unit)
            sync.synced_through = until
            sync.last_success_at = moment
            sync.last_error = None
        if added:
            logger.debug("sensors.synced", sensor=key, measurement=name, added=added)
        return added

    async def _store(
        self,
        repo: SensorRepository,
        key: str,
        name: str,
        cursor: datetime,
        history: list[tuple[datetime, str]],
        unit: str | None,
    ) -> int:
        last = await repo.last_state(key, name)
        added = 0

        def changed(value: float | None, status: str) -> bool:
            return last is None or last.status != status or last.value != value

        def add(at: datetime, value: float | None, status: str, raw: str | None) -> None:
            nonlocal last, added
            if last is not None and at <= last.started_at:
                # Two transitions inside one stored instant: keep the order by
                # nudging the later one, rather than colliding on the key.
                at = last.started_at + timedelta(microseconds=1)
            row = SensorState(
                sensor_key=key,
                measurement=name,
                started_at=at,
                value=value,
                status=status,
                raw_state=raw[:255] if raw is not None else None,
                unit=unit,
            )
            repo.add_state(row)
            last = row
            added += 1

        if not history or history[0][0] > cursor + _START_SLACK:
            # HA holds nothing for [cursor, first entry). Mark it, once.
            if changed(None, SensorStatus.NO_RECORD):
                add(cursor, None, SensorStatus.NO_RECORD, None)

        for at, raw in history:
            value, status = classify(raw)
            if changed(value, status):
                add(max(at, cursor), value, status, raw)
        return added
