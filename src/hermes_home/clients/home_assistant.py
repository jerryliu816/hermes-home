"""Home Assistant REST client.

Home Assistant is the device abstraction layer, so this is the only place that
speaks to it and there is no vendor-specific code anywhere in the project. If
every Eufy camera were replaced with another brand tomorrow, nothing here would
change -- only the entity IDs in ``config/cameras.yaml``.

Three endpoints matter for v1:

``GET /api/states/{entity_id}``      entity state and attributes
``GET /api/camera_proxy/{entity}``   a live snapshot from a camera entity
``GET /api/image_proxy/{entity}``    the current bytes of an ``image.*`` entity
``GET /api/history/period/{start}``  recorded state changes for an entity

The last one is the important one. An ``image.*`` entity's *state* is an ISO 8601
timestamp of when its image last changed, which is what lets us tell a fresh
event still from the previous event's frame.

The access token is never logged, never formatted into an exception message, and
never returned in an error body.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from hermes_home.core.errors import HomeAssistantError
from hermes_home.core.time import ensure_utc, parse_ha_timestamp

logger = structlog.get_logger(__name__)

# Bounded, and deliberately small. Home Assistant must never be kept waiting on
# us, and an unavailable HA is a condition to record, not to retry forever.
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.5


@dataclass(frozen=True)
class EntityState:
    """A Home Assistant entity's state at a point in time."""

    entity_id: str
    state: str
    attributes: dict[str, Any]
    last_changed: datetime | None
    last_updated: datetime | None

    def state_as_timestamp(self) -> datetime | None:
        """Interpret the state as an ISO 8601 timestamp.

        For ``image.*`` entities the state *is* the time the image last changed.
        Returns None when the state is not a timestamp (``unknown``,
        ``unavailable``, or a different entity domain).
        """
        if self.state in ("unknown", "unavailable", "", "None"):
            return None
        try:
            return parse_ha_timestamp(self.state)
        except (ValueError, TypeError):
            return None


class HomeAssistantClient:
    """Async client for the Home Assistant REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Held only to build the auth header; never logged or stringified.
        self._token = token
        self._timeout = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HomeAssistantClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----------------------------------------------------------------- #

    async def _get(self, path: str) -> httpx.Response:
        """GET with bounded retry.

        Retries only transient conditions. A 401 means the token is wrong and
        will still be wrong in half a second, so it fails immediately.
        """
        url = f"{self._base_url}{path}"
        last_error: HomeAssistantError | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.get(url, headers=self._headers)
            except httpx.TimeoutException as exc:
                last_error = HomeAssistantError(
                    "ha_timeout", f"timed out requesting {path}", retryable=True
                )
                logger.warning("ha.request.timeout", path=path, attempt=attempt, error=str(exc))
            except httpx.HTTPError as exc:
                last_error = HomeAssistantError(
                    "ha_connection", f"connection error requesting {path}", retryable=True
                )
                logger.warning("ha.request.error", path=path, attempt=attempt, error=str(exc))
            else:
                if response.status_code == 401:
                    # Deliberately says nothing about the token itself.
                    raise HomeAssistantError(
                        "ha_auth",
                        "Home Assistant rejected our credentials (401); check HOME_ASSISTANT_TOKEN",
                        retryable=False,
                    )
                if response.status_code == 404:
                    raise HomeAssistantError(
                        "ha_not_found", f"Home Assistant has no such endpoint/entity: {path}"
                    )
                if response.status_code >= 500:
                    last_error = HomeAssistantError(
                        "ha_server_error",
                        f"Home Assistant returned {response.status_code} for {path}",
                        retryable=True,
                    )
                    logger.warning(
                        "ha.request.server_error",
                        path=path,
                        attempt=attempt,
                        status=response.status_code,
                    )
                elif response.status_code >= 400:
                    raise HomeAssistantError(
                        "ha_client_error",
                        f"Home Assistant returned {response.status_code} for {path}",
                    )
                else:
                    return response

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_SECONDS * attempt)

        assert last_error is not None
        raise last_error

    # ----------------------------------------------------------------- #

    async def get_entity_state(self, entity_id: str) -> EntityState:
        """Fetch one entity's current state and attributes."""
        response = await self._get(f"/api/states/{entity_id}")
        data = response.json()

        def _maybe_ts(key: str) -> datetime | None:
            raw = data.get(key)
            if not raw:
                return None
            try:
                return parse_ha_timestamp(raw)
            except (ValueError, TypeError):
                return None

        return EntityState(
            entity_id=data.get("entity_id", entity_id),
            state=str(data.get("state", "")),
            attributes=data.get("attributes") or {},
            last_changed=_maybe_ts("last_changed"),
            last_updated=_maybe_ts("last_updated"),
        )

    async def get_camera_image(self, camera_entity_id: str) -> tuple[bytes, str]:
        """Live snapshot from a ``camera.*`` entity. Returns (bytes, media_type)."""
        response = await self._get(f"/api/camera_proxy/{camera_entity_id}")
        return response.content, response.headers.get("content-type", "image/jpeg")

    async def get_image_entity_image(self, image_entity_id: str) -> tuple[bytes, str]:
        """Current bytes of an ``image.*`` entity, e.g. a camera's event still.

        This is the preferred path for camera events: it is the frame the
        integration already captured, so it costs no battery, no P2P stream, and
        no wake-up on the camera itself.
        """
        response = await self._get(f"/api/image_proxy/{image_entity_id}")
        return response.content, response.headers.get("content-type", "image/jpeg")

    async def get_state_history(
        self, entity_id: str, *, start: datetime, end: datetime | None = None
    ) -> list[tuple[datetime, str]]:
        """Recorded ``(changed_at, state)`` pairs for one entity, oldest first.

        Home Assistant's recorder is what lets us tell *one* missed event from
        several: it keeps a timestamp per transition, so four lost deliveries
        are reported as four rather than as "something went wrong". Without it
        the best we could say is that the last known state differs from ours.

        ``minimal_response`` keeps the payload to states and timestamps -- no
        attributes, and never an image. A history call costs the camera nothing.

        Bounded by the recorder's own retention: nothing before it is knowable,
        which is why a period with no history reads as unknown, not as clean.
        """
        params = {
            "filter_entity_id": entity_id,
            "minimal_response": "",
            "significant_changes_only": "0",
        }
        if end is not None:
            params["end_time"] = ensure_utc(end).isoformat()
        query = urlencode(params)
        path = f"/api/history/period/{ensure_utc(start).isoformat()}?{query}"

        response = await self._get(path)
        payload = response.json()
        if not payload:
            return []

        history: list[tuple[datetime, str]] = []
        for series in payload:
            for entry in series:
                raw = entry.get("last_changed") or entry.get("last_updated")
                state = entry.get("state")
                if not raw or state is None:
                    continue
                try:
                    history.append((parse_ha_timestamp(raw), str(state)))
                except (ValueError, TypeError):
                    continue
        history.sort(key=lambda item: item[0])
        return history

    async def ping(self) -> bool:
        """True when the API answers and our token is accepted."""
        try:
            await self._get("/api/")
        except HomeAssistantError:
            return False
        return True
