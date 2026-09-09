"""The camera health monitor.

Its own asyncio task, deliberately not part of the ingest worker: health must
keep being observed while ingestion is idle, and a failure here must never
affect webhook acceptance or vision processing.

What it reads is entity *state* only -- never a camera image. A health check
costs no battery, no P2P stream, and retains nothing.

The one subtle thing in this module is that it writes to two places with
different rules:

    camera_health            debounced   -- what to tell someone asking now
    camera_health_intervals  eager       -- what actually happened

Debouncing history would let a genuinely observed outage disappear from the
record, which is the single failure this feature exists to prevent. Debouncing
the current status only avoids announcing an outage on one dropped request. So
they differ, they can briefly disagree, and that is correct.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CameraConfig, CamerasConfig, Settings
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.models import CameraHealth, HealthReason, HealthStatus
from hermes_home.storage.repositories import CameraHealthRepository

logger = structlog.get_logger(__name__)

#: HA states that mean "this entity is not currently providing anything".
_UNAVAILABLE = "unavailable"
_UNKNOWN = "unknown"

#: Client error codes that mean Home Assistant itself could not be reached, as
#: opposed to answering with bad news about a camera. The distinction matters:
#: HA being down is our blindness, not the camera's failure.
_HA_DOWN_CODES = frozenset({"ha_timeout", "ha_connection", "ha_server_error", "ha_auth"})


@dataclass(frozen=True)
class Observation:
    """One poll's verdict about one camera, before any debouncing."""

    status: str
    reason: str | None
    camera_state: str | None = None
    image_state: str | None = None
    image_updated_at: datetime | None = None


def _normalize(state: str | None) -> str:
    return (state or "").strip().casefold()


def evaluate(
    camera: CameraConfig,
    *,
    health_state: str | None,
    health_missing: bool = False,
    image_state: str | None = None,
    image_missing: bool = False,
    image_updated_at: datetime | None = None,
) -> Observation:
    """Classify one camera from the entity states just read.

    Pure and synchronous so the rules can be tested without a monitor, a
    database, or a clock. Order matters and is the order written down in the
    plan: availability first, state predicate second.
    """
    entity = camera.health_check_entity()
    if entity is None:
        # Nothing to poll. Not a failure of the camera -- a gap in what we were
        # told about it -- so it is unknown rather than offline.
        return Observation(HealthStatus.UNKNOWN, HealthReason.NO_HEALTH_ENTITY)

    normalized = _normalize(health_state)

    # 1. Availability. Never overridden by a state predicate: an entity that is
    #    unavailable is telling us nothing, whatever states we configured.
    if health_missing:
        return Observation(
            HealthStatus.OFFLINE, HealthReason.CAMERA_ENTITY_NOT_FOUND, camera_state=health_state
        )
    if normalized == _UNAVAILABLE:
        return Observation(
            HealthStatus.OFFLINE,
            HealthReason.CAMERA_ENTITY_UNAVAILABLE,
            camera_state=health_state,
        )

    # 2. State predicate, only when one was configured. Without it the semantics
    #    are exactly what they were before this feature existed.
    if camera.health_healthy_states:
        healthy_states = {_normalize(s) for s in camera.health_healthy_states}
        if normalized == _UNKNOWN:
            # HA's own not-determinable marker. Not evidence of disconnection,
            # and the tri-state model has the honest answer available.
            return Observation(
                HealthStatus.UNKNOWN,
                HealthReason.HEALTH_ENTITY_STATE_UNKNOWN,
                camera_state=health_state,
            )
        if normalized not in healthy_states:
            return Observation(
                HealthStatus.OFFLINE,
                HealthReason.HEALTH_ENTITY_UNHEALTHY_STATE,
                camera_state=health_state,
            )

    # 3. Supporting entity. A camera whose event-image entity is unavailable is
    #    reachable but cannot produce an analyzable frame, which is degraded.
    #    An image state of "unknown" is NOT degraded: that is the ordinary
    #    reading after a Home Assistant reload, until the camera next fires.
    if camera.event_image_strategy == "image_entity_state" and camera.event_image_entity:
        if image_missing or _normalize(image_state) == _UNAVAILABLE:
            return Observation(
                HealthStatus.DEGRADED,
                HealthReason.EVENT_IMAGE_ENTITY_UNAVAILABLE,
                camera_state=health_state,
                image_state=image_state,
                image_updated_at=image_updated_at,
            )

    return Observation(
        HealthStatus.HEALTHY,
        None,
        camera_state=health_state,
        image_state=image_state,
        image_updated_at=image_updated_at,
    )


class CameraHealthMonitor:
    """Polls entity availability and records health over time."""

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
        #: Tracked so HA going down and coming back is logged once, not once per
        #: camera per minute.
        self._ha_reachable: bool | None = None
        #: True until the first poll completes, so startup always splits any
        #: interval left open by the previous process.
        self._starting = True

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._settings.camera_health_enabled:
            logger.info("camera_health.disabled")
            return
        self._task = asyncio.create_task(self._run(), name="camera-health")
        logger.info(
            "camera_health.started",
            interval_seconds=self._settings.camera_health_interval_seconds,
            cameras=len(self._cameras.cameras),
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    # ----------------------------------------------------------------- #

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never take down hermes-home. A malformed HA payload or a bug
                # here costs one polling interval, nothing more.
                logger.exception("camera_health.poll_failed")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._settings.camera_health_interval_seconds,
                )
            except TimeoutError:
                continue

    async def poll_once(self) -> dict[str, Observation]:
        """One full sweep across every configured camera."""
        moment = now_utc()
        observations: dict[str, Observation] = {}

        for key, camera in self._cameras.cameras.items():
            observations[key] = await self._observe(camera)

        reachable = any(o.reason != HealthReason.HA_UNREACHABLE for o in observations.values())
        if observations and self._ha_reachable is not reachable:
            if self._ha_reachable is not None:
                logger.warning(
                    "home_assistant.health_changed",
                    old_status="reachable" if self._ha_reachable else "unreachable",
                    new_status="reachable" if reachable else "unreachable",
                )
            self._ha_reachable = reachable

        async with session_scope(self._session_factory) as session:
            repo = CameraHealthRepository(session)
            for key, observation in observations.items():
                await self._record(repo, key, observation, moment)

        self._starting = False
        return observations

    async def _observe(self, camera: CameraConfig) -> Observation:
        """Read the entities this camera's health depends on."""
        entity = camera.health_check_entity()
        if entity is None:
            return evaluate(camera, health_state=None)

        try:
            state = await self._ha.get_entity_state(entity)
        except HomeAssistantError as exc:
            if exc.code in _HA_DOWN_CODES:
                return Observation(HealthStatus.UNKNOWN, HealthReason.HA_UNREACHABLE)
            return evaluate(camera, health_state=None, health_missing=True)

        image_state: str | None = None
        image_missing = False
        image_updated_at: datetime | None = None
        wants_image = (
            camera.event_image_strategy == "image_entity_state" and camera.event_image_entity
        )
        if wants_image and camera.event_image_entity != entity:
            try:
                image = await self._ha.get_entity_state(camera.event_image_entity)
            except HomeAssistantError as exc:
                if exc.code in _HA_DOWN_CODES:
                    return Observation(HealthStatus.UNKNOWN, HealthReason.HA_UNREACHABLE)
                image_missing = True
            else:
                image_state = image.state
                image_updated_at = image.state_as_timestamp()
        elif wants_image:
            image_state = state.state
            image_updated_at = state.state_as_timestamp()

        return evaluate(
            camera,
            health_state=state.state,
            image_state=image_state,
            image_missing=image_missing,
            image_updated_at=image_updated_at,
        )

    # ----------------------------------------------------------------- #

    async def _record(
        self,
        repo: CameraHealthRepository,
        camera_key: str,
        observation: Observation,
        moment: datetime,
    ) -> None:
        await self._record_history(repo, camera_key, observation, moment)
        await self._record_current(repo, camera_key, observation, moment)

    async def _record_history(
        self,
        repo: CameraHealthRepository,
        camera_key: str,
        observation: Observation,
        moment: datetime,
    ) -> None:
        """Append to the interval history. Never debounced.

        Three cases, in order of subtlety:

        * status unchanged, and we were watching continuously -> extend
        * status unchanged, but there is a hole in our observation -> SPLIT
          anyway. Healthy before and healthy after proves nothing about the
          middle, and stitching them together would manufacture evidence.
        * status changed -> close and open
        """
        open_interval = await repo.open_interval(camera_key)
        if open_interval is None:
            await repo.start_interval(
                camera_key=camera_key,
                status=observation.status,
                reason=observation.reason,
                at=moment,
            )
            return

        elapsed = (moment - open_interval.observed_through).total_seconds()
        observed_continuously = (
            not self._starting and elapsed <= self._settings.camera_health_gap_tolerance_seconds
        )

        if open_interval.status == observation.status and observed_continuously:
            await repo.extend_interval(open_interval, at=moment)
            return

        # Closed at ``moment`` only when observation never lapsed: the status
        # then genuinely held until the poll that saw it change. After a gap it
        # closes at its last confirmed observation instead, and the untouched
        # span becomes an explicit unknown.
        await repo.close_interval(open_interval, at=moment if observed_continuously else None)
        await repo.start_interval(
            camera_key=camera_key,
            status=observation.status,
            reason=observation.reason,
            at=moment,
        )

    async def _record_current(
        self,
        repo: CameraHealthRepository,
        camera_key: str,
        observation: Observation,
        moment: datetime,
    ) -> None:
        """Update the debounced current-status row."""
        row = await repo.get(camera_key)
        if row is None:
            row = CameraHealth(
                camera_key=camera_key,
                status=observation.status,
                pending_status=None,
                observed_status=observation.status,
                reason=observation.reason,
                checked_at=moment,
                consecutive_failures=0,
                first_observed_at=moment,
            )
            repo.add_current(row)
            previous = None
        else:
            previous = row.status

        row.observed_status = observation.status
        row.checked_at = moment
        row.camera_state = observation.camera_state
        row.image_state = observation.image_state
        if observation.image_updated_at is not None:
            row.last_image_update_at = observation.image_updated_at

        if observation.status == HealthStatus.HEALTHY:
            # Recovery commits immediately. Delaying good news only widens a gap
            # that has already ended.
            row.consecutive_failures = 0
            row.pending_status = None
            row.status = HealthStatus.HEALTHY
            row.reason = None
            row.last_healthy_at = moment
            row.offline_since = None
        else:
            if row.pending_status == observation.status:
                row.consecutive_failures += 1
            else:
                row.pending_status = observation.status
                row.consecutive_failures = 1

            if row.consecutive_failures >= self._settings.camera_health_failure_threshold:
                row.status = observation.status
                row.reason = observation.reason
                row.pending_status = None
                if observation.status == HealthStatus.OFFLINE and row.offline_since is None:
                    row.offline_since = moment

        if previous is not None and previous != row.status:
            logger.warning(
                "camera.health_changed",
                camera=camera_key,
                old_status=previous,
                new_status=row.status,
                reason=row.reason,
            )
