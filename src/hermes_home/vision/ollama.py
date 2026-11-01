"""Local vision provider: a model served by Ollama on this machine.

Uses Ollama's **native** ``/api/chat`` endpoint rather than the OpenAI-compatible
``/v1`` one, for two reasons that both matter here:

* ``format`` takes the :class:`SceneObservation` JSON schema itself, so decoding
  is constrained to the same schema the Anthropic path is held to.
* The response says whether generation actually finished (``done`` and
  ``done_reason``). A reply cut off at the token limit is a truncated JSON
  document, and it must be rejected rather than half-parsed.

What counts as success is deliberately narrow. The reply must be a complete,
non-streamed envelope for the model we asked for, whose ``message.content``
validates as a ``SceneObservation`` exactly as sent. Prose, fenced JSON, a
reasoning trace in ``message.thinking``, or an object with an extra field are
all failures. Nothing is repaired, and nothing missing is filled in -- a count
the model left out stays null, which is what the schema's defaults already mean.

Every attempt runs under one total deadline (``VisionRequest.timeout_seconds``)
covering the queue behind other Ollama clients, a cold model load and
generation. HTTPX's own timeouts only bound individual reads, which a slow but
steady stream would never trip.

Failure messages are fixed strings. Nothing from a response body, an exception,
the prompt or the image is copied into a stored error or a log line, so a
camera's private context or a misconfigured proxy's error page cannot end up in
the event history.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Literal

import httpx
import structlog
from pydantic import ValidationError

from hermes_home.domain.observations import SceneObservation
from hermes_home.vision.base import (
    RETRYABLE_CODES,
    VisionErrorCode,
    VisionRequest,
    VisionResult,
    VisionStatus,
)
from hermes_home.vision.prompts import (
    OLLAMA_JSON_GUIDANCE,
    OLLAMA_PROMPT_VERSION,
    build_system_prompt,
    build_user_prompt,
)

logger = structlog.get_logger(__name__)

ThinkSetting = Literal["omit", "true", "false"]


def _observation_format_schema() -> dict[str, Any]:
    """The ``SceneObservation`` schema with every property marked required.

    Types are unchanged, so every field that may be null still may be -- the
    model can and should answer null for what the frame does not show. Only key
    *presence* is forced. Measured on gemma4:31b (Ollama 0.40.13, MLX runner):
    with only ``scene_summary`` required, the constrained output simply omitted
    lighting, importance, overall_confidence and activity on every real frame,
    and the schema defaults then recorded them as null -- an "unknown" the model
    never said. With all keys required it filled them (daylight, low_light, ...)
    and still used null for counts it could not see.
    """
    schema = SceneObservation.model_json_schema()
    schema["required"] = list(schema["properties"])
    return schema


#: Computed once: the schema Ollama constrains decoding to. Sent as an object,
#: never a string -- a string ``format`` means something else to Ollama.
OBSERVATION_SCHEMA: dict[str, Any] = _observation_format_schema()

#: A complete observation is a few kilobytes. Anything near this is not one,
#: and reading it all would only spend memory finding that out.
MAX_RESPONSE_BYTES = 1024 * 1024
#: /api/show carries the license and template text; still small, still bounded.
MAX_METADATA_BYTES = 4 * 1024 * 1024
#: Diagnostics are a metadata lookup; they never wait on inference.
DIAGNOSTIC_TIMEOUT_SECONDS = 5.0

_CONNECT_TIMEOUT_SECONDS = 5.0
_WRITE_TIMEOUT_SECONDS = 10.0
_POOL_TIMEOUT_SECONDS = 5.0

#: done_reason values worth naming in a stored message. Anything else is
#: reported as "other" rather than echoed.
_KNOWN_DONE_REASONS = frozenset({"length", "load", "unload"})


class _ResponseTooLarge(Exception):
    """The body exceeded its cap and was abandoned unread."""


class _BadResponse(Exception):
    """A response that arrived but is not a usable observation.

    ``message`` is always one of this module's fixed strings.
    """

    def __init__(self, message: str, usage: dict[str, int | None] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.usage = usage or {"input_tokens": None, "output_tokens": None}


class OllamaVisionProvider:
    """Scene analysis via a local Ollama vision model."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        num_ctx: int = 8192,
        num_predict: int = 2048,
        think: ThinkSetting = "omit",
        keep_alive: str | None = None,
        client: httpx.AsyncClient | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if not model or not model.strip():
            raise ValueError("OllamaVisionProvider requires an explicit model name")
        if think not in ("omit", "true", "false"):
            raise ValueError("think must be 'omit', 'true' or 'false'")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._num_ctx = num_ctx
        self._num_predict = num_predict
        self._think = think
        self._keep_alive = keep_alive
        self._max_response_bytes = max_response_bytes
        # An injected client belongs to whoever injected it; only a client made
        # here is closed here.
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT_SECONDS,
                read=None,  # set per request; the total deadline is asyncio's
                write=_WRITE_TIMEOUT_SECONDS,
                pool=_POOL_TIMEOUT_SECONDS,
            ),
            follow_redirects=False,
            # Proxy variables are for reaching the internet. Routing a frame
            # meant for a model on this machine through one would be a leak.
            trust_env=False,
        )

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ----------------------------------------------------------------- #
    # Analysis
    # ----------------------------------------------------------------- #

    def build_payload(self, request: VisionRequest) -> dict[str, Any]:
        """The exact JSON body sent to ``/api/chat``. Public so tests and the
        probe command can show it without a network call."""
        payload: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "messages": [
                {"role": "system", "content": build_system_prompt(request.context)},
                {
                    "role": "user",
                    "content": build_user_prompt(request.context) + OLLAMA_JSON_GUIDANCE,
                    # Bare base64; Ollama does not accept a data: URL prefix.
                    "images": [base64.b64encode(request.image).decode("ascii")],
                },
            ],
            "format": OBSERVATION_SCHEMA,
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": self._num_predict,
                "temperature": 0,
            },
        }
        if self._think != "omit":
            payload["think"] = self._think == "true"
        if self._keep_alive is not None:
            payload["keep_alive"] = self._keep_alive
        return payload

    async def analyze(self, request: VisionRequest) -> VisionResult:
        started = time.monotonic()
        payload = self.build_payload(request)
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=request.timeout_seconds,
            write=_WRITE_TIMEOUT_SECONDS,
            pool=_POOL_TIMEOUT_SECONDS,
        )
        # CancelledError is deliberately never caught below: shutdown and lease
        # handling depend on cancellation actually cancelling.
        try:
            async with asyncio.timeout(request.timeout_seconds):
                status, body = await self._post(
                    "/api/chat", payload, http_timeout=timeout, limit=self._max_response_bytes
                )
        except TimeoutError:
            return self._failure(
                VisionErrorCode.TIMEOUT,
                "ollama did not finish within the analysis deadline",
                started,
                error_type="deadline",
            )
        except httpx.TimeoutException as exc:
            return self._failure(
                VisionErrorCode.TIMEOUT,
                "ollama request timed out",
                started,
                error_type=type(exc).__name__,
            )
        except httpx.TransportError as exc:
            # Includes connection refused: Ollama not running is an outage to
            # record, and never an empty scene.
            return self._failure(
                VisionErrorCode.SERVER_ERROR,
                "could not reach ollama",
                started,
                error_type=type(exc).__name__,
            )
        except _ResponseTooLarge:
            return self._failure(
                VisionErrorCode.BAD_RESPONSE,
                "ollama response exceeded the size limit",
                started,
                error_type="response_too_large",
            )
        except Exception as exc:  # anything else is still data, never a raise
            return self._failure(
                VisionErrorCode.UNEXPECTED,
                "unexpected error calling ollama",
                started,
                error_type=type(exc).__name__,
            )

        if status != 200:
            code, message = _classify_status(status)
            return self._failure(code, message, started, http_status=status)

        try:
            observation, usage = self._parse(body)
        except _BadResponse as exc:
            return self._failure(
                VisionErrorCode.BAD_RESPONSE,
                exc.message,
                started,
                error_type="bad_response",
                usage=exc.usage,
            )

        return VisionResult(
            status=VisionStatus.OK,
            observation=observation,
            provider=self.name,
            model=self._model,
            prompt_version=OLLAMA_PROMPT_VERSION,
            latency_ms=_elapsed_ms(started),
            # API cost only. A local model is not free -- hardware, electricity
            # and the memory it holds are real -- but none of it is billed per call.
            cost_usd=0.0,
            **usage,
        )

    def _parse(self, body: bytes) -> tuple[SceneObservation, dict[str, int | None]]:
        try:
            envelope = json.loads(body)
        except ValueError:
            raise _BadResponse("ollama response was not valid JSON") from None
        if not isinstance(envelope, dict):
            raise _BadResponse("ollama response envelope was not a JSON object")

        usage = _usage(envelope)
        if "error" in envelope:
            raise _BadResponse("ollama reported an error in a successful response", usage)
        if envelope.get("done") is not True:
            raise _BadResponse("ollama response was incomplete (done was not true)", usage)
        done_reason = envelope.get("done_reason")
        if done_reason is not None and done_reason != "stop":
            named = done_reason if done_reason in _KNOWN_DONE_REASONS else "other"
            raise _BadResponse(
                f"ollama generation did not finish normally (done_reason={named})", usage
            )
        # The row records the model we asked for. If the server says it answered
        # with another one, recording ours would misattribute the observation.
        if envelope.get("model") != self._model:
            raise _BadResponse("ollama response named a different model than requested", usage)

        message = envelope.get("message")
        if not isinstance(message, dict):
            raise _BadResponse("ollama response had no message object", usage)
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            # A reply whose only text is in message.thinking lands here too: a
            # reasoning trace is not an observation, however plausible it reads.
            raise _BadResponse("ollama response had no message content", usage)

        try:
            parsed = json.loads(content)
        except ValueError:
            raise _BadResponse("model output was not valid JSON", usage) from None
        if not isinstance(parsed, dict):
            raise _BadResponse("model output was not a JSON object", usage)
        try:
            observation = SceneObservation.model_validate_json(content)
        except ValidationError:
            raise _BadResponse("model output did not match the observation schema", usage) from None
        return observation, usage

    def _failure(
        self,
        code: VisionErrorCode,
        message: str,
        started: float,
        *,
        error_type: str | None = None,
        http_status: int | None = None,
        usage: dict[str, int | None] | None = None,
    ) -> VisionResult:
        logger.warning(
            "vision.ollama.failed",
            error_code=code.value,
            error_type=error_type,
            http_status=http_status,
            model=self._model,
        )
        return VisionResult(
            status=VisionStatus.FAILED,
            provider=self.name,
            model=self._model,
            prompt_version=OLLAMA_PROMPT_VERSION,
            latency_ms=_elapsed_ms(started),
            error_code=code,
            error_message=message,
            retryable=code in RETRYABLE_CODES,
            **(usage or {}),
        )

    async def _post(
        self, path: str, payload: dict[str, Any], *, http_timeout: httpx.Timeout, limit: int
    ) -> tuple[int, bytes]:
        """POST and read the body under a byte cap. Error bodies are not read."""
        async with self._client.stream(
            "POST", f"{self._base_url}{path}", json=payload, timeout=http_timeout
        ) as response:
            if response.status_code != 200:
                return response.status_code, b""
            return response.status_code, await _read_capped(response, limit)

    # ----------------------------------------------------------------- #
    # Diagnostics. Metadata only: nothing here loads a model or runs inference.
    # ----------------------------------------------------------------- #

    async def health_check(self) -> bool:
        """Whether the configured model is installed and reports vision support.

        Deliberately not wired into /health or /ready: a stopped Ollama must
        make analyses fail visibly, not make Docker restart a service whose
        durable inbox is still accepting events.
        """
        report = await self.diagnose()
        return bool(report["model_present"] and report["vision"])

    async def diagnose(self) -> dict[str, Any]:
        """A bounded, secret-free description of what Ollama says about the model."""
        report: dict[str, Any] = {
            "base_url": self._base_url,
            "model": self._model,
            "reachable": False,
            "version": None,
            "model_present": False,
            "capabilities": [],
            "vision": False,
            "thinking": False,
            "think_setting": self._think,
            "think_setting_ok": None,
            "details": {},
            "error": None,
        }
        timeout = httpx.Timeout(DIAGNOSTIC_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout(DIAGNOSTIC_TIMEOUT_SECONDS * 2):
                async with self._client.stream(
                    "GET", f"{self._base_url}/api/version", timeout=timeout
                ) as response:
                    report["reachable"] = True
                    if response.status_code == 200:
                        version = json.loads(await _read_capped(response, 64 * 1024))
                        if isinstance(version, dict) and isinstance(version.get("version"), str):
                            report["version"] = version["version"][:64]
                status, body = await self._post(
                    "/api/show",
                    {"model": self._model},
                    http_timeout=timeout,
                    limit=MAX_METADATA_BYTES,
                )
        except (TimeoutError, httpx.TimeoutException):
            report["error"] = "timed out"
            return report
        except httpx.TransportError:
            report["error"] = "unreachable"
            return report
        except Exception:
            report["error"] = "unexpected response"
            return report

        if status == 404:
            report["error"] = "model not installed"
            return report
        if status != 200:
            report["error"] = f"HTTP {status} from /api/show"
            return report
        try:
            show = json.loads(body)
        except ValueError:
            report["error"] = "unexpected response"
            return report
        if not isinstance(show, dict):
            report["error"] = "unexpected response"
            return report

        report["model_present"] = True
        capabilities = show.get("capabilities")
        if isinstance(capabilities, list):
            report["capabilities"] = [c for c in capabilities if isinstance(c, str)][:32]
        report["vision"] = "vision" in report["capabilities"]
        report["thinking"] = "thinking" in report["capabilities"]
        # Ollama rejects a think flag for a model without the capability.
        report["think_setting_ok"] = self._think == "omit" or report["thinking"]
        details = show.get("details")
        if isinstance(details, dict):
            report["details"] = {
                key: details[key]
                for key in ("family", "parameter_size", "quantization_level", "format", "runner")
                if isinstance(details.get(key), str)
            }
        return report


# --------------------------------------------------------------------------- #


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise _ResponseTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _classify_status(status: int) -> tuple[VisionErrorCode, str]:
    """Map a non-200 status onto the shared vocabulary, with a fixed message.

    Only plausibly transient statuses are retryable. A missing model or an
    option the model does not support fails identically on every attempt.
    """
    if status == 429:
        return VisionErrorCode.RATE_LIMITED, "ollama is busy (HTTP 429)"
    if status >= 500:
        return VisionErrorCode.SERVER_ERROR, f"ollama server error (HTTP {status})"
    if status in (401, 403):
        return VisionErrorCode.AUTH, f"ollama refused authorization (HTTP {status})"
    if status == 404:
        return (
            VisionErrorCode.UNEXPECTED,
            "ollama returned HTTP 404: model not installed or endpoint missing",
        )
    if status == 400:
        return (
            VisionErrorCode.UNEXPECTED,
            "ollama rejected the request (HTTP 400): unsupported image, schema or "
            "option for this model",
        )
    return VisionErrorCode.UNEXPECTED, f"unexpected HTTP status from ollama ({status})"


def _usage(envelope: dict[str, Any]) -> dict[str, int | None]:
    def count(key: str) -> int | None:
        value = envelope.get(key)
        # bool is an int subclass; True is not a token count.
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    return {"input_tokens": count("prompt_eval_count"), "output_tokens": count("eval_count")}


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
