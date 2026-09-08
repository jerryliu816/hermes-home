"""Home Assistant client: success, auth failure, timeout, and retry bounds."""

from __future__ import annotations

import httpx
import pytest

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.core.errors import HomeAssistantError

BASE = "http://ha.test"


def _client(handler) -> HomeAssistantClient:
    transport = httpx.MockTransport(handler)
    return HomeAssistantClient(BASE, "secret-token", client=httpx.AsyncClient(transport=transport))


async def test_get_entity_state_parses_image_timestamp() -> None:
    """An image.* entity's state IS a timestamp -- the freshness gate depends on it."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        return httpx.Response(
            200,
            json={
                "entity_id": "image.front_door_event_image",
                "state": "2026-09-08T12:00:00+00:00",
                "attributes": {"entity_picture": "/api/image_proxy/image.front_door"},
                "last_changed": "2026-09-08T12:00:00+00:00",
                "last_updated": "2026-09-08T12:00:00+00:00",
            },
        )

    async with _client(handler) as client:
        state = await client.get_entity_state("image.front_door_event_image")

    assert state.state_as_timestamp() is not None
    assert state.state_as_timestamp().year == 2026
    assert state.attributes["entity_picture"].startswith("/api/image_proxy/")


async def test_unavailable_state_has_no_timestamp() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entity_id": "image.x", "state": "unavailable"})

    async with _client(handler) as client:
        state = await client.get_entity_state("image.x")

    assert state.state_as_timestamp() is None


async def test_get_image_entity_image_returns_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/image_proxy/image.front_door_event_image"
        return httpx.Response(
            200, content=b"\xff\xd8jpegbytes", headers={"content-type": "image/jpeg"}
        )

    async with _client(handler) as client:
        data, media_type = await client.get_image_entity_image("image.front_door_event_image")

    assert data == b"\xff\xd8jpegbytes"
    assert media_type == "image/jpeg"


async def test_auth_failure_is_immediate_and_leaks_no_token() -> None:
    """401 must not be retried, and the error must never contain the token."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"message": "Unauthorized"})

    async with _client(handler) as client:
        with pytest.raises(HomeAssistantError) as excinfo:
            await client.get_entity_state("image.x")

    assert excinfo.value.code == "ha_auth"
    assert excinfo.value.retryable is False
    assert calls == 1, "a wrong token will still be wrong in half a second"
    assert "secret-token" not in str(excinfo.value)


async def test_timeout_retries_then_raises_retryable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("too slow", request=request)

    async with _client(handler) as client:
        with pytest.raises(HomeAssistantError) as excinfo:
            await client.get_entity_state("image.x")

    assert excinfo.value.code == "ha_timeout"
    assert excinfo.value.retryable is True
    assert calls == 3, "retries must be bounded, not infinite"


async def test_server_error_recovers_on_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"entity_id": "image.x", "state": "unknown"})

    async with _client(handler) as client:
        state = await client.get_entity_state("image.x")

    assert state.entity_id == "image.x"
    assert calls == 2


async def test_missing_entity_raises_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    async with _client(handler) as client:
        with pytest.raises(HomeAssistantError) as excinfo:
            await client.get_entity_state("image.nope")

    assert excinfo.value.code == "ha_not_found"
