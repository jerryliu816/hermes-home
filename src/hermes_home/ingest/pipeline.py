"""The ingest orchestrator: everything that happens after HTTP 202.

    delivery -> [A] delivery dedupe -> [A'] freshness gate -> fetch image
             -> [B] content dedupe  -> analyze (+1 bounded retry)
             -> persist event -> tag -> [C] correlate

Ordering is not arbitrary. The image fetch is milliseconds and free; the vision
call is seconds and metered. So we hash the bytes and check for a duplicate
*before* spending an analysis -- that is the entire reason stage B sits where it
does.

This module holds no FastAPI import and no HTTP concepts, so the whole pipeline
is testable with a fake Home Assistant client and the mock vision provider.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CameraConfig, CamerasConfig, Settings
from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.ids import content_hash as compute_content_hash
from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc, parse_ha_timestamp
from hermes_home.domain.event_types import get_spec
from hermes_home.ingest.correlate import correlate
from hermes_home.ingest.freshness import StaleImageError, fetch_event_image
from hermes_home.spatial import resolve_zone_id, zone_by_key
from hermes_home.storage.models import (
    Disposition,
    Event,
    EventAnalysis,
    EventDelivery,
)
from hermes_home.storage.repositories import (
    EventRepository,
    IncidentRepository,
)
from hermes_home.vision.base import (
    CameraEventContext,
    VisionProvider,
    VisionRequest,
    VisionResult,
    VisionStatus,
)
from hermes_home.vision.prompts import PROMPT_VERSION

logger = structlog.get_logger(__name__)

#: One retry, not an exponential ladder. If a second immediate attempt does not
#: work, the condition is not transient and the delivery-level backoff handles it.
_VISION_RETRY_DELAY_SECONDS = 2.0


@dataclass(frozen=True)
class IngestOutcome:
    disposition: str
    event_uid: str | None
    note: str | None = None


class ProcessingError(Exception):
    """Transient failure: the delivery should be retried later."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def process_delivery(
    session: AsyncSession,
    delivery: EventDelivery,
    *,
    settings: Settings,
    cameras: CamerasConfig,
    ha_client: HomeAssistantClient,
    vision: VisionProvider,
) -> IngestOutcome:
    """Turn one accepted delivery into a stored event, or explain why not."""
    log = logger.bind(
        correlation_id=delivery.correlation_id,
        delivery_uid=delivery.uid,
        attempt=delivery.attempts,
    )

    body = delivery.raw_body or {}
    event_type = body["event_type"]
    camera_key = body["camera"]
    occurred_at = _parse_occurred_at(body)

    spec = get_spec(event_type)
    camera = cameras.cameras.get(camera_key)
    if camera is None:
        return IngestOutcome(Disposition.REJECTED_INVALID, None, f"unknown camera {camera_key!r}")

    events = EventRepository(session)
    incidents = IncidentRepository(session)

    # --- [A] delivery dedupe ------------------------------------------------
    # Cheap pre-check. The real guarantee is the UNIQUE constraint at insert.
    existing = await events.find_by_delivery_key(delivery.delivery_key)
    if existing is not None:
        await events.increment_duplicate_count(existing)
        log.info("ingest.duplicate_delivery", event_uid=existing.uid)
        return IngestOutcome(Disposition.DUPLICATE_DELIVERY, existing.uid)

    trigger_entity = _trigger_entity(body, camera)

    # --- [A'] freshness gate + image retrieval ------------------------------
    image = None
    if spec.needs_image:
        last_seen = (
            await events.latest_source_state_ts(camera.event_image_entity)
            if camera.event_image_entity
            else None
        )
        try:
            image = await fetch_event_image(
                ha_client,
                camera,
                occurred_at=occurred_at,
                last_seen_state_ts=last_seen,
                poll_attempts=settings.freshness_poll_attempts,
                poll_interval_seconds=settings.freshness_poll_interval_seconds,
                tolerance_seconds=settings.freshness_tolerance_seconds,
            )
        except StaleImageError as exc:
            log.warning("ingest.stale_image", reason=str(exc))
            return IngestOutcome(Disposition.REJECTED_STALE_IMAGE, None, str(exc))
        except HomeAssistantError as exc:
            if exc.retryable:
                raise ProcessingError(exc.code, str(exc)) from exc
            # Non-retryable: keep the event, record that analysis never happened.
            log.warning("ingest.image_unavailable", error_code=exc.code)
            image = None

    # --- [A''] stale-frame fallback ------------------------------------------
    # When the image entity published no usable timestamp we could not confirm
    # the frame advanced. That is routine right after a Home Assistant restart
    # or integration reload: the entity resets to "unknown" while image_proxy
    # keeps serving the previous event's picture. Identical bytes to the last
    # event for this entity means we are looking at that stale frame, and
    # storing it under the current time would be a plausible-looking lie.
    image_hash = compute_content_hash(image.data) if image else None
    if image is not None and not image.freshness_verified and image_hash:
        previous_hash = await events.latest_content_hash(trigger_entity)
        if previous_hash == image_hash:
            log.warning(
                "ingest.stale_image_unchanged",
                entity_id=trigger_entity,
                reason="no entity timestamp and bytes identical to the previous event",
            )
            return IngestOutcome(
                Disposition.REJECTED_STALE_IMAGE,
                None,
                "event image entity published no timestamp and served the "
                "previous event's frame unchanged",
            )

    # --- [B] content dedupe -------------------------------------------------
    if image_hash:
        twin = await events.find_recent_by_content_hash(
            source_entity_id=trigger_entity,
            content_hash=image_hash,
            window_seconds=settings.dedupe_window_seconds,
        )
        if twin is not None:
            # Identical bytes moments ago: one occurrence reaching us twice.
            # Crucially, we have not called the vision provider yet.
            await events.increment_duplicate_count(twin)
            log.info("ingest.duplicate_content", event_uid=twin.uid)
            return IngestOutcome(Disposition.DUPLICATE_CONTENT, twin.uid)

    # --- analysis -----------------------------------------------------------
    results: list[VisionResult] = []
    if image is not None:
        zone = await zone_by_key(session, camera.location)
        context = CameraEventContext(
            camera_key=camera_key,
            camera_name=camera.name,
            zone_name=zone.name if zone else camera.location,
            observes=camera.observes,
            event_type=event_type,
            occurred_at=occurred_at,
        )
        results = await _analyze_with_retry(
            vision,
            VisionRequest(
                image=image.data,
                media_type=image.media_type,
                context=context,
                timeout_seconds=settings.vision_timeout_seconds,
            ),
            log=log,
        )

    # The image is deliberately not persisted anywhere; it goes out of scope here
    # along with the last reference to its bytes.

    # --- persist ------------------------------------------------------------
    zone_id = await resolve_zone_id(session, trigger_entity)
    if zone_id is None:
        zone = await zone_by_key(session, camera.location)
        zone_id = zone.id if zone else None

    moment = now_utc()
    payload = spec.payload_model.model_validate(
        {
            "camera": camera_key,
            "trigger_entity": trigger_entity,
            "event_image_entity": camera.event_image_entity,
            "camera_entity": camera.camera_entity,
            "metadata": body.get("metadata") or {},
        }
    )

    event = Event(
        uid=new_uid(),
        delivery_key=delivery.delivery_key,
        event_type=event_type,
        source=delivery.source,
        source_entity_id=trigger_entity,
        zone_id=zone_id,
        occurred_at=occurred_at,
        received_at=delivery.received_at,
        source_state_ts=image.source_state_ts if image else None,
        content_hash=image_hash,
        duplicate_count=0,
        payload=payload.model_dump(mode="json"),
        payload_schema_version=spec.payload_schema_version,
        created_at=moment,
    )

    try:
        await events.create(event)
    except IntegrityError:
        # The UNIQUE constraint on delivery_key fired: a concurrent worker or a
        # replay beat us here. This -- not the queue -- is what makes ingestion
        # exactly-once.
        await session.rollback()
        winner = await events.find_by_delivery_key(delivery.delivery_key)
        if winner is not None:
            await events.increment_duplicate_count(winner)
            return IngestOutcome(Disposition.DUPLICATE_DELIVERY, winner.uid)
        raise

    dimensions = _image_dimensions(image.data) if image else None
    for index, result in enumerate(results, start=1):
        await events.add_analysis(
            _to_analysis_row(
                event.id,
                index,
                result,
                moment,
                artifact_bytes=len(image.data) if image else None,
                dimensions=dimensions,
            )
        )

    final = results[-1] if results else None
    if final is not None and final.ok and final.observation is not None:
        await events.add_tags(event.id, final.observation.tags, source="vision")

    # --- [C] correlation ----------------------------------------------------
    await correlate(
        incidents,
        event,
        window_seconds=settings.correlation_window_seconds,
        occurred_at=occurred_at,
    )

    log.info(
        "ingest.persisted",
        event_uid=event.uid,
        event_type=event_type,
        zone_id=zone_id,
        analysis_status=final.status.value if final else "skipped",
        vision_provider=final.provider if final else None,
        vision_model=final.model if final else None,
        latency_ms=final.latency_ms if final else None,
    )
    return IngestOutcome(Disposition.ACCEPTED, event.uid)


async def _analyze_with_retry(
    vision: VisionProvider, request: VisionRequest, *, log: structlog.BoundLogger
) -> list[VisionResult]:
    """Analyze, retrying once for transient failures.

    Returns every attempt, so a transient failure followed by a success leaves
    both rows and the flake rate is visible without extra instrumentation.
    """
    attempts: list[VisionResult] = []
    for attempt in (1, 2):
        try:
            result = await vision.analyze(request)
        except Exception as exc:  # a provider bug must not lose the event
            log.exception("vision.provider_raised", error_type=type(exc).__name__)
            result = VisionResult(
                status=VisionStatus.FAILED,
                provider=getattr(vision, "name", "unknown"),
                prompt_version=PROMPT_VERSION,
                error_code="unexpected",
                error_message=f"{type(exc).__name__}: {exc}"[:1000],
                retryable=False,
            )
        attempts.append(result)

        if result.status is not VisionStatus.FAILED or not result.retryable:
            break
        if attempt == 1:
            log.warning("vision.retrying", error_code=result.error_code)
            await asyncio.sleep(_VISION_RETRY_DELAY_SECONDS)
    return attempts


def _to_analysis_row(
    event_id: int,
    attempt: int,
    result: VisionResult,
    started: datetime,
    *,
    artifact_bytes: int | None = None,
    dimensions: tuple[int, int] | None = None,
) -> EventAnalysis:
    observation = result.observation
    return EventAnalysis(
        event_id=event_id,
        attempt=attempt,
        kind="vision_scene",
        status=result.status.value,
        provider=result.provider,
        model=result.model,
        prompt_version=result.prompt_version,
        observation=observation.to_stored_json() if observation else None,
        error_code=result.error_code.value if result.error_code else None,
        error_message=result.error_message,
        retryable=result.retryable,
        started_at=started,
        completed_at=now_utc(),
        latency_ms=result.latency_ms,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        artifact_bytes=artifact_bytes,
        artifact_width=dimensions[0] if dimensions else None,
        artifact_height=dimensions[1] if dimensions else None,
    )


def _trigger_entity(body: dict[str, object], camera: CameraConfig) -> str | None:
    raw = body.get("entity_id") or body.get("source_entity")
    if isinstance(raw, str) and raw:
        return raw
    return camera.event_image_entity or camera.camera_entity


def _parse_occurred_at(body: dict[str, object]) -> datetime:
    raw = body.get("timestamp")
    if isinstance(raw, str) and raw:
        return parse_ha_timestamp(raw)
    return now_utc()


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read pixel dimensions from the header without decoding the image.

    Worth the twenty lines because the image itself is discarded: once it is
    gone, this is the only record of what was actually analyzed, and we would
    rather not pull in Pillow for two integers.
    """
    # PNG: width/height are big-endian uint32 at bytes 16..24.
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )

    # JPEG: walk the segment markers to the start-of-frame.
    if data[:2] == b"\xff\xd8":
        index = 2
        end = len(data)
        while index + 9 < end:
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            # SOF0..SOF15, excluding the non-frame markers DHT/JPG/DAC.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    int.from_bytes(data[index + 7 : index + 9], "big"),
                    int.from_bytes(data[index + 5 : index + 7], "big"),
                )
            segment_length = int.from_bytes(data[index + 2 : index + 4], "big")
            if segment_length <= 0:
                break
            index += 2 + segment_length
    return None
