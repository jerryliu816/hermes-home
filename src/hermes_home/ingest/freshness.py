"""Getting the *right* frame, not just a frame.

Camera integrations publish an event still asynchronously, so a Home Assistant
automation can easily reach us before the image entity has caught up. Analyzing
whatever is there at that moment means describing the previous event and filing
it under the current timestamp -- a wrong answer that looks entirely plausible in
the database, which is the worst kind.

An ``image.*`` entity's state is an ISO 8601 timestamp of its last change, so we
can wait for it to advance. That is expected Eufy behavior and is verified
against real hardware before the first production run; other brands may differ,
which is why the strategy is per-camera configuration rather than an assumption
baked into the pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CameraConfig
from hermes_home.core.errors import ImageUnavailableError
from hermes_home.core.time import ensure_utc

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FetchedImage:
    data: bytes
    media_type: str
    #: The image entity's own timestamp, when the strategy provides one.
    source_state_ts: datetime | None
    #: True when we positively confirmed this frame is newer than the last one
    #: we consumed. False means "could not tell" -- the caller must fall back to
    #: comparing content before trusting it. See the no-timestamp case below.
    freshness_verified: bool = False


class StaleImageError(ImageUnavailableError):
    """The event image never advanced; we refuse to analyze a stale frame."""

    def __init__(self, message: str) -> None:
        super().__init__("stale_image", message, retryable=False)


async def fetch_event_image(
    client: HomeAssistantClient,
    camera: CameraConfig,
    *,
    occurred_at: datetime,
    last_seen_state_ts: datetime | None,
    poll_attempts: int,
    poll_interval_seconds: float,
    tolerance_seconds: float = 5.0,
) -> FetchedImage | None:
    """Retrieve the still for this event, honoring the camera's strategy.

    Returns None when the camera is configured not to produce images at all.
    """
    if camera.event_image_strategy == "none":
        return None

    if camera.event_image_strategy == "camera_snapshot":
        assert camera.camera_entity
        data, media_type = await client.get_camera_image(camera.camera_entity)
        # A live snapshot is by definition current; there is nothing to verify.
        return FetchedImage(
            data=data, media_type=media_type, source_state_ts=None, freshness_verified=True
        )

    # image_entity_state: wait for the entity's timestamp to move past what we
    # last consumed, then fetch.
    assert camera.event_image_entity
    entity_id = camera.event_image_entity

    state_ts: datetime | None = None
    verified = False

    # The frame must belong to THIS event, so the test is against the trigger
    # time, not against what we happened to store last.
    #
    # An earlier version asked only "is this newer than the last frame we
    # consumed", which silently accepts a stale image whenever the previous
    # event has no recorded timestamp -- observed on real hardware attaching a
    # 255-second-old frame to a fresh trigger. Both timestamps here come from
    # Home Assistant's own clock, so the tolerance only needs to absorb an
    # integration that stamps at capture rather than at receipt.
    floor = ensure_utc(occurred_at) - timedelta(seconds=tolerance_seconds)
    if last_seen_state_ts is not None and last_seen_state_ts > floor:
        # Never reuse a frame already consumed by an earlier event.
        floor = last_seen_state_ts

    # Measured on real Eufy hardware: the event still lands ~4s AFTER the
    # detection trigger, because the camera has to upload it first, and the
    # entity reports "unknown" in the meantime. So "unknown" means "not here
    # yet", not "give up". Waiting is free -- the webhook was acknowledged long
    # ago and this runs on the background worker.
    for attempt in range(1, poll_attempts + 1):
        state = await client.get_entity_state(entity_id)
        state_ts = state.state_as_timestamp()

        if state_ts is not None and state_ts > floor:
            verified = True
            break

        if attempt < poll_attempts:
            logger.debug(
                "ingest.freshness.waiting",
                entity_id=entity_id,
                attempt=attempt,
                state=state.state,
                waited_seconds=round(attempt * poll_interval_seconds, 2),
            )
            await asyncio.sleep(poll_interval_seconds)

    if not verified:
        waited = round(poll_attempts * poll_interval_seconds, 1)
        if state_ts is None:
            # Never published a usable timestamp. Fetch anyway rather than lose a
            # real event -- the caller compares the bytes against the previous
            # event before trusting this frame.
            logger.info(
                "ingest.freshness.no_timestamp",
                entity_id=entity_id,
                state=state.state,
                waited_seconds=waited,
            )
        else:
            age = round((ensure_utc(occurred_at) - state_ts).total_seconds(), 1)
            raise StaleImageError(
                f"event image {entity_id} is {age}s older than the trigger "
                f"(frame {state_ts.isoformat()}, trigger "
                f"{ensure_utc(occurred_at).isoformat()}) after waiting {waited}s"
            )

    data, media_type = await client.get_image_entity_image(entity_id)
    return FetchedImage(
        data=data,
        media_type=media_type,
        source_state_ts=state_ts,
        freshness_verified=verified,
    )
