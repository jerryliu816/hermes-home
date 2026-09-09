"""The inbound webhook.

Home Assistant must never wait on us. This endpoint authenticates, validates,
commits the delivery, and returns 202 -- everything expensive happens on the
worker. If the vision provider is down, or Home Assistant's image pipeline is
slow, or this service is restarting, the HA automation still completes promptly
and the home keeps working.

Durability before acknowledgement: the 202 is only sent once the delivery row is
committed, so an accepted event survives a crash one millisecond later.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from hermes_home.api.deps import AppState, get_state
from hermes_home.core.ids import delivery_key as compute_delivery_key
from hermes_home.core.ids import new_uid
from hermes_home.core.time import now_utc, parse_ha_timestamp
from hermes_home.domain.event_types import is_known, known_event_types
from hermes_home.storage.engine import confirm_delivery_durable, session_scope
from hermes_home.storage.repositories import DeliveryRepository

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/v1/events", tags=["events"])

SECRET_HEADER = "X-Hermes-Webhook-Secret"


class HomeAssistantEvent(BaseModel):
    """What the Home Assistant automation posts.

    Note what is *not* here: the image. HA sends metadata; we fetch the still
    ourselves. That keeps the automation fast and avoids pushing image bytes
    through the automation engine.
    """

    model_config = ConfigDict(extra="allow")

    event_type: str = Field(max_length=64)
    camera: str = Field(max_length=64)
    entity_id: str | None = Field(default=None, max_length=255)
    source_entity: str | None = Field(default=None, max_length=255)
    timestamp: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AcceptedResponse(BaseModel):
    status: str = "accepted"
    delivery_uid: str
    correlation_id: str


async def _verify_secret(
    state: Annotated[AppState, Depends(get_state)],
    secret: Annotated[str | None, Header(alias=SECRET_HEADER)] = None,
) -> None:
    """Constant-time comparison, so timing cannot leak the secret."""
    expected = state.settings.webhook_secret
    provided = secret or ""
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid webhook secret"
        )


@router.post(
    "/home-assistant",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    dependencies=[Depends(_verify_secret)],
)
async def receive_home_assistant_event(
    request: Request,
    payload: HomeAssistantEvent,
    response: Response,
    state: Annotated[AppState, Depends(get_state)],
) -> AcceptedResponse:
    correlation_id = new_uid()
    log = logger.bind(correlation_id=correlation_id)

    if not is_known(payload.event_type):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown event_type; known types: {known_event_types()}",
        )

    received_at = now_utc()
    occurred_at = received_at
    if payload.timestamp:
        try:
            occurred_at = parse_ha_timestamp(payload.timestamp)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="timestamp must be ISO 8601 with a UTC offset",
            ) from None

    trigger_entity = payload.entity_id or payload.source_entity
    key = compute_delivery_key(
        source="home_assistant",
        event_type=payload.event_type,
        source_entity_id=trigger_entity,
        occurred_at=occurred_at,
    )

    # Only the JSON body is stored. Headers are never persisted -- they carry
    # the shared secret.
    body = payload.model_dump(mode="json")
    body["timestamp"] = occurred_at.isoformat()

    async with session_scope(state.session_factory) as session:
        delivery = await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=key,
            correlation_id=correlation_id,
            raw_body=body,
            received_at=received_at,
        )
        delivery_uid = delivery.uid

    # A commit that returns success is not proof the row exists. On 2026-09-09
    # four deliveries were committed without error, acknowledged to Home
    # Assistant with 202, and then simply were not there -- and because we had
    # already promised success, Home Assistant had no way to know the events
    # were gone. It logged nothing, retried nothing, and the history acquired a
    # hole shaped exactly like a quiet morning.
    #
    # So the promise is verified before it is made, through a connection that
    # did not perform the write: a row is always visible to its own writer, so
    # only a fresh connection can attest that it is really there.
    #
    # No retry. If the database cannot confirm a write it just accepted, the
    # honest move is to fail loudly and let Home Assistant surface it -- its
    # rest_command logs a warning on any non-2xx, which is exactly the signal
    # that was missing.
    verify_engine = state.verify_engine or state.engine
    if not await confirm_delivery_durable(verify_engine, delivery_uid):
        log.error(
            "webhook.durability_unconfirmed",
            delivery_uid=delivery_uid,
            event_type=payload.event_type,
            camera=payload.camera,
            detail="commit reported success but the row was not visible to a fresh connection",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="delivery could not be confirmed durable; not accepted",
        )

    log.info(
        "webhook.accepted",
        delivery_uid=delivery_uid,
        event_type=payload.event_type,
        camera=payload.camera,
        client=request.client.host if request.client else None,
    )

    # Wake the worker rather than let it wait out the poll interval.
    if state.worker is not None:
        state.worker.notify()

    response.headers["X-Correlation-Id"] = correlation_id
    return AcceptedResponse(delivery_uid=delivery_uid, correlation_id=correlation_id)
