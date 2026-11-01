"""Ollama provider: request shape, strict response validation, failure mapping.

No network. Every test drives the real provider over ``httpx.MockTransport``,
so what is asserted is the exact bytes that would reach Ollama and the exact
result that would be persisted. Whether a real model on the real runner accepts
this payload is a separate question, answered only by the opt-in live probe
(``hermes-home vision probe``) -- these tests cannot and do not claim it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from hermes_home.core.time import now_utc
from hermes_home.domain.observations import SceneObservation
from hermes_home.vision.base import (
    CameraEventContext,
    VisionErrorCode,
    VisionRequest,
    VisionStatus,
)
from hermes_home.vision.ollama import OBSERVATION_SCHEMA, OllamaVisionProvider
from hermes_home.vision.prompts import (
    OLLAMA_JSON_GUIDANCE,
    OLLAMA_PROMPT_VERSION,
    PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
)
from tests.conftest import TINY_IMAGE

BASE_URL = "http://ollama.test:11434"
MODEL = "gemma4:31b"

#: Strings that must never surface in a stored error or a log line.
SECRET = "sk-ant-SECRET-SENTINEL-0123456789"
PRIVATE_CONTEXT = "PRIVATE-CAMERA-CONTEXT-SENTINEL"

GOOD_OBSERVATION = {
    "scene_summary": "An empty porch in daylight.",
    "person_count": 0,
    "vehicle_count": None,
    "animal_count": None,
    "package_present": False,
    "activity": "no_activity",
    "lighting": "daylight",
    "importance": "low",
    "overall_confidence": "high",
    "notable_attributes": [],
    "tags": [],
    "field_notes": {"vehicle_count": "driveway out of frame"},
}


def _context(**overrides) -> CameraEventContext:
    values = {
        "camera_key": "front_door",
        "camera_name": "Front Door",
        "zone_name": "Front Entry",
        "observes": ["front_entry"],
        "event_type": "camera.person_detected",
        "occurred_at": now_utc(),
    }
    values.update(overrides)
    return CameraEventContext(**values)


def _request(*, image: bytes = TINY_IMAGE, timeout: float = 5.0, **ctx) -> VisionRequest:
    return VisionRequest(
        image=image, media_type="image/png", context=_context(**ctx), timeout_seconds=timeout
    )


def _envelope(content: object = GOOD_OBSERVATION, **overrides) -> dict:
    body = {
        "model": MODEL,
        "created_at": "2026-10-31T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": content if isinstance(content, str) else json.dumps(content),
        },
        "done": True,
        "done_reason": "stop",
        "total_duration": 1_000_000,
        "prompt_eval_count": 812,
        "eval_count": 143,
    }
    body.update(overrides)
    return body


class Recorder:
    """A MockTransport handler that records requests and replays responses."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], object]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if callable(nxt) and not isinstance(nxt, httpx.Response):
            result = nxt(request)
            if asyncio.iscoroutine(result):
                result = await result
            return result  # type: ignore[return-value]
        return nxt

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def _provider(handler, **kwargs) -> tuple[OllamaVisionProvider, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    options = {"base_url": BASE_URL, "model": MODEL, "client": client}
    options.update(kwargs)
    return OllamaVisionProvider(**options), client


def _ok(**overrides) -> httpx.Response:
    return httpx.Response(200, json=_envelope(**overrides))


# --------------------------------------------------------------------------- #
# Request shape
# --------------------------------------------------------------------------- #


async def test_request_goes_to_native_chat_endpoint_without_streaming() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder)
    await provider.analyze(_request())

    sent = recorder.requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == f"{BASE_URL}/api/chat"
    assert "/v1" not in str(sent.url)
    body = recorder.body()
    assert body["model"] == MODEL
    assert body["stream"] is False


async def test_image_is_bare_base64_of_the_exact_bytes() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder)
    await provider.analyze(_request())

    images = recorder.body()["messages"][1]["images"]
    assert images == [base64.b64encode(TINY_IMAGE).decode("ascii")]
    assert not images[0].startswith("data:")
    assert base64.b64decode(images[0]) == TINY_IMAGE


async def test_format_is_the_observation_schema_object_not_a_string() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder)
    await provider.analyze(_request())

    fmt = recorder.body()["format"]
    assert isinstance(fmt, dict)
    assert fmt == OBSERVATION_SCHEMA
    assert fmt.get("additionalProperties") is False


def test_format_schema_requires_every_key_but_keeps_nulls_allowed() -> None:
    """Key presence is forced so a model cannot leave a field out and have the
    default record an unknown it never stated; the types are untouched."""
    original = SceneObservation.model_json_schema()
    assert OBSERVATION_SCHEMA["properties"] == original["properties"]
    assert set(OBSERVATION_SCHEMA["required"]) == set(original["properties"])
    for field in ("person_count", "lighting", "overall_confidence", "activity"):
        types = [a.get("type") for a in OBSERVATION_SCHEMA["properties"][field]["anyOf"]]
        assert "null" in types, field
    # The pydantic model itself still accepts omitted optional fields.
    assert SceneObservation.model_json_schema()["required"] == ["scene_summary"]


async def test_options_are_bounded_and_deterministic() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder, num_ctx=8192, num_predict=2048)
    await provider.analyze(_request())

    assert recorder.body()["options"] == {"num_ctx": 8192, "num_predict": 2048, "temperature": 0}


async def test_prompts_reuse_the_shared_builders() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder)
    request = _request()
    await provider.analyze(request)

    system, user = recorder.body()["messages"]
    assert system == {"role": "system", "content": build_system_prompt(request.context)}
    assert user["role"] == "user"
    assert user["content"] == build_user_prompt(request.context) + OLLAMA_JSON_GUIDANCE
    assert "images" not in system


@pytest.mark.parametrize(("setting", "expected"), [("true", True), ("false", False)])
async def test_think_is_sent_only_when_explicitly_configured(setting, expected) -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder, think=setting)
    await provider.analyze(_request())
    assert recorder.body()["think"] is expected


async def test_think_and_keep_alive_are_omitted_by_default() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder)
    await provider.analyze(_request())
    body = recorder.body()
    assert "think" not in body
    assert "keep_alive" not in body


async def test_keep_alive_is_per_request_when_configured() -> None:
    recorder = Recorder(_ok())
    provider, _ = _provider(recorder, keep_alive="30m")
    await provider.analyze(_request())
    assert recorder.body()["keep_alive"] == "30m"


def test_an_explicit_model_is_required() -> None:
    with pytest.raises(ValueError):
        OllamaVisionProvider(base_url=BASE_URL, model=" ")


# --------------------------------------------------------------------------- #
# Successful responses
# --------------------------------------------------------------------------- #


async def test_valid_observation_becomes_an_ok_result_with_provenance() -> None:
    provider, _ = _provider(Recorder(_ok()))
    result = await provider.analyze(_request())

    assert result.ok
    assert result.status is VisionStatus.OK
    assert result.provider == "ollama"
    assert result.model == MODEL
    assert result.prompt_version == OLLAMA_PROMPT_VERSION
    assert result.prompt_version != PROMPT_VERSION
    assert result.input_tokens == 812
    assert result.output_tokens == 143
    assert result.cost_usd == 0.0
    assert result.latency_ms >= 0
    assert result.observation == SceneObservation.model_validate(GOOD_OBSERVATION)


async def test_unknowns_stay_null_and_omitted_optionals_take_null_defaults() -> None:
    content = {"scene_summary": "Frame too dark to tell.", "person_count": None}
    provider, _ = _provider(Recorder(_ok(content=content)))
    result = await provider.analyze(_request())

    assert result.ok and result.observation is not None
    stored = result.observation.to_stored_json()
    for field in ("person_count", "vehicle_count", "animal_count", "package_present"):
        assert stored[field] is None, field  # never coerced to 0/False


async def test_missing_done_reason_is_accepted_when_done_is_true() -> None:
    envelope = _envelope()
    del envelope["done_reason"]
    provider, _ = _provider(Recorder(httpx.Response(200, json=envelope)))
    assert (await provider.analyze(_request())).ok


@pytest.mark.parametrize("bad", [-1, True, "812", 1.5])
async def test_implausible_token_counts_are_dropped_not_stored(bad) -> None:
    provider, _ = _provider(Recorder(_ok(prompt_eval_count=bad)))
    result = await provider.analyze(_request())
    assert result.ok
    assert result.input_tokens is None


async def test_the_model_s_thinking_is_never_read_as_the_observation() -> None:
    envelope = _envelope(content="")
    envelope["message"]["thinking"] = json.dumps(GOOD_OBSERVATION)
    provider, _ = _provider(Recorder(httpx.Response(200, json=envelope)))
    result = await provider.analyze(_request())
    assert not result.ok
    assert result.error_code is VisionErrorCode.BAD_RESPONSE


# --------------------------------------------------------------------------- #
# Responses that must be rejected
# --------------------------------------------------------------------------- #

_BAD_ENVELOPES: dict[str, httpx.Response] = {
    "not json": httpx.Response(200, content=b"<html>proxy error</html>"),
    "array envelope": httpx.Response(200, json=[_envelope()]),
    "done false": _ok(done=False),
    "done missing": httpx.Response(200, json={k: v for k, v in _envelope().items() if k != "done"}),
    "truncated at token limit": _ok(
        done_reason="length", content=json.dumps(GOOD_OBSERVATION)[:40]
    ),
    "unloaded mid-request": _ok(done_reason="unload"),
    "different model echoed": _ok(model="gemma4-64k:latest"),
    "model missing": httpx.Response(
        200, json={k: v for k, v in _envelope().items() if k != "model"}
    ),
    "no message": httpx.Response(
        200, json={k: v for k, v in _envelope().items() if k != "message"}
    ),
    "empty content": _ok(content=""),
    "whitespace content": _ok(content="   \n"),
    "prose content": _ok(content="I can see an empty porch with no people."),
    "fenced json": _ok(content="```json\n" + json.dumps(GOOD_OBSERVATION) + "\n```"),
    "json array content": _ok(content="[1, 2]"),
    "extra field": _ok(content={**GOOD_OBSERVATION, "person_name": "Jerry"}),
    "negative count": _ok(content={**GOOD_OBSERVATION, "person_count": -1}),
    "bad enum": _ok(content={**GOOD_OBSERVATION, "lighting": "moonlight"}),
    "count as string": _ok(content={**GOOD_OBSERVATION, "person_count": "two"}),
    "missing summary": _ok(
        content={k: v for k, v in GOOD_OBSERVATION.items() if k != "scene_summary"}
    ),
    "error field": _ok(error="something"),
}


@pytest.mark.parametrize("case", sorted(_BAD_ENVELOPES))
async def test_unusable_responses_are_terminal_bad_responses(case: str) -> None:
    provider, _ = _provider(Recorder(_BAD_ENVELOPES[case]))
    result = await provider.analyze(_request())

    assert result.status is VisionStatus.FAILED, case
    assert result.observation is None, case
    assert result.error_code is VisionErrorCode.BAD_RESPONSE, case
    assert result.retryable is False, case
    assert result.provider == "ollama"
    assert result.model == MODEL
    assert result.prompt_version == OLLAMA_PROMPT_VERSION


async def test_oversized_response_is_rejected_without_reading_it_all() -> None:
    huge = b'{"padding": "' + b"x" * 4096 + b'"}'
    provider, _ = _provider(Recorder(httpx.Response(200, content=huge)), max_response_bytes=1024)
    result = await provider.analyze(_request())
    assert result.error_code is VisionErrorCode.BAD_RESPONSE
    assert result.retryable is False


async def test_bad_response_keeps_token_usage_when_the_envelope_had_it() -> None:
    provider, _ = _provider(Recorder(_ok(done_reason="length")))
    result = await provider.analyze(_request())
    assert result.input_tokens == 812
    assert result.output_tokens == 143


# --------------------------------------------------------------------------- #
# HTTP and transport failures
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, VisionErrorCode.UNEXPECTED, False),  # unsupported vision/schema/think
        (401, VisionErrorCode.AUTH, False),
        (403, VisionErrorCode.AUTH, False),
        (404, VisionErrorCode.UNEXPECTED, False),  # model not installed
        (422, VisionErrorCode.UNEXPECTED, False),
        (429, VisionErrorCode.RATE_LIMITED, True),
        (500, VisionErrorCode.SERVER_ERROR, True),
        (503, VisionErrorCode.SERVER_ERROR, True),
        (307, VisionErrorCode.UNEXPECTED, False),  # redirects are not followed
    ],
)
async def test_http_status_mapping(status, code, retryable) -> None:
    response = httpx.Response(
        status, json={"error": f"model {SECRET} not found"}, headers={"location": "http://x"}
    )
    provider, _ = _provider(Recorder(response))
    result = await provider.analyze(_request())

    assert result.status is VisionStatus.FAILED
    assert result.error_code is code
    assert result.retryable is retryable
    assert SECRET not in (result.error_message or "")
    assert str(status) in (result.error_message or "")


async def test_connection_refused_is_a_retryable_outage_not_an_empty_scene() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused {SECRET}", request=request)

    provider, _ = _provider(Recorder(refuse))
    result = await provider.analyze(_request())
    assert result.status is VisionStatus.FAILED
    assert result.observation is None
    assert result.error_code is VisionErrorCode.SERVER_ERROR
    assert result.retryable is True
    assert SECRET not in (result.error_message or "")


async def test_http_read_timeout_is_a_retryable_timeout() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider, _ = _provider(Recorder(slow))
    result = await provider.analyze(_request())
    assert result.error_code is VisionErrorCode.TIMEOUT
    assert result.retryable is True


async def test_total_deadline_bounds_the_whole_attempt() -> None:
    """A server that never answers is cut off by the attempt deadline itself."""

    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return _ok()

    provider, _ = _provider(Recorder(hang))
    loop = asyncio.get_running_loop()
    started = loop.time()
    result = await provider.analyze(_request(timeout=0.2))
    elapsed = loop.time() - started

    assert elapsed < 2.0
    assert result.error_code is VisionErrorCode.TIMEOUT
    assert result.retryable is True
    assert "deadline" in (result.error_message or "")


async def test_unexpected_client_exception_becomes_data_without_its_text() -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"boom {SECRET}")

    provider, _ = _provider(Recorder(explode))
    result = await provider.analyze(_request())
    assert result.error_code is VisionErrorCode.UNEXPECTED
    assert result.retryable is False
    assert SECRET not in (result.error_message or "")


async def test_ollama_never_produces_a_refusal() -> None:
    """Ollama has no refusal signal; declining prose is a bad response, not REFUSED."""
    provider, _ = _provider(Recorder(_ok(content="I'm sorry, I can't help with that.")))
    result = await provider.analyze(_request())
    assert result.status is VisionStatus.FAILED
    assert result.error_code is VisionErrorCode.BAD_RESPONSE


# --------------------------------------------------------------------------- #
# Cancellation and lifecycle
# --------------------------------------------------------------------------- #


async def test_cancellation_propagates_and_is_not_turned_into_a_result() -> None:
    entered = asyncio.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.sleep(30)
        return _ok()

    provider, _ = _provider(Recorder(hang))
    task = asyncio.create_task(provider.analyze(_request(timeout=60)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_aclose_closes_an_owned_client() -> None:
    provider = OllamaVisionProvider(base_url=BASE_URL, model=MODEL)
    client = provider._client
    assert not client.is_closed
    await provider.aclose()
    assert client.is_closed


async def test_aclose_leaves_an_injected_client_open() -> None:
    provider, client = _provider(Recorder(_ok()))
    await provider.aclose()
    assert not client.is_closed
    await client.aclose()


def test_owned_client_ignores_proxy_environment_and_redirects() -> None:
    provider = OllamaVisionProvider(base_url=BASE_URL, model=MODEL)
    assert provider._client.trust_env is False
    assert provider._client.follow_redirects is False
    asyncio.run(provider.aclose())


# --------------------------------------------------------------------------- #
# Secrets and private data never leak into stored errors or logs
# --------------------------------------------------------------------------- #


async def test_failures_leak_nothing_private_into_messages_or_logs() -> None:
    image = TINY_IMAGE + SECRET.encode()
    encoded = base64.b64encode(image).decode("ascii")
    responses = [
        httpx.Response(500, json={"error": SECRET}, headers={"x-debug": SECRET}),
        httpx.Response(400, text=f"bad option {SECRET} {PRIVATE_CONTEXT}"),
        _ok(content=f"prose mentioning {PRIVATE_CONTEXT} and {SECRET}"),
        _ok(content={**GOOD_OBSERVATION, "secret": SECRET}),
        httpx.Response(200, content=f"not json {SECRET}".encode()),
    ]
    for response in responses:
        provider, _ = _provider(Recorder(response))
        with capture_logs() as logs:
            result = await provider.analyze(_request(image=image, vision_context=PRIVATE_CONTEXT))
        rendered = json.dumps(result.model_dump(mode="json")) + json.dumps(logs, default=str)
        for needle in (SECRET, PRIVATE_CONTEXT, encoded[:40]):
            assert needle not in rendered, (response.status_code, needle)


async def test_the_provider_writes_no_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = sorted(os.listdir(tmp_path))
    provider, _ = _provider(Recorder(_ok()))
    await provider.analyze(_request())
    provider, _ = _provider(Recorder(httpx.Response(500)))
    await provider.analyze(_request())
    assert sorted(os.listdir(tmp_path)) == before


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def _show_handler(show_status: int = 200, capabilities=("completion", "vision", "thinking")):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.40.13"})
        assert request.url.path == "/api/show"
        assert json.loads(request.content) == {"model": MODEL}
        if show_status != 200:
            return httpx.Response(show_status, json={"error": "not found"})
        return httpx.Response(
            200,
            json={
                "capabilities": list(capabilities),
                "details": {"family": "gemma4", "format": "safetensors", "runner": "mlx"},
                "license": "x" * 1000,
            },
        )

    return handler


async def test_health_check_true_for_installed_vision_model() -> None:
    provider, _ = _provider(_show_handler())
    assert await provider.health_check() is True
    report = await provider.diagnose()
    assert report["version"] == "0.40.13"
    assert report["vision"] and report["thinking"]
    assert report["details"]["runner"] == "mlx"
    assert "license" not in report


async def test_health_check_false_for_a_text_only_model() -> None:
    provider, _ = _provider(_show_handler(capabilities=("completion", "tools")))
    assert await provider.health_check() is False
    assert (await provider.diagnose())["vision"] is False


async def test_health_check_false_for_a_missing_model() -> None:
    provider, _ = _provider(_show_handler(show_status=404))
    assert await provider.health_check() is False
    assert (await provider.diagnose())["error"] == "model not installed"


async def test_health_check_false_when_ollama_is_down() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = _provider(refuse)
    assert await provider.health_check() is False
    report = await provider.diagnose()
    assert report["reachable"] is False
    assert report["error"] == "unreachable"


async def test_diagnose_flags_a_think_setting_the_model_cannot_take() -> None:
    provider, _ = _provider(_show_handler(capabilities=("completion", "vision")), think="false")
    report = await provider.diagnose()
    assert report["vision"] is True
    assert report["think_setting_ok"] is False


async def test_diagnostics_never_call_inference() -> None:
    recorder = Recorder(_show_handler())
    provider, _ = _provider(recorder)
    await provider.diagnose()
    assert {r.url.path for r in recorder.requests} <= {"/api/version", "/api/show"}
