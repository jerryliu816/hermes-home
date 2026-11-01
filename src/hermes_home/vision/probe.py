"""One opt-in, live analysis through the real provider code, and nothing else.

Used by ``hermes-home vision probe`` to verify a provider end to end before it
is selected for real events: the actual payload, the actual network call, and
the actual ``SceneObservation`` validation. It never opens the database, never
writes a file, and never fetches a camera image. The frame is either generated
here or a file the operator chose to pass.

A generated frame proves plumbing -- the runner accepted an image together with
a JSON schema, and the reply validated -- not accuracy. Judging accuracy needs
real frames, judged by a person (see docs/operations.md).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

from hermes_home.config import Settings, load_cameras_config
from hermes_home.core.time import now_utc
from hermes_home.vision.base import CameraEventContext, VisionProvider, VisionRequest

#: Large enough that a vision encoder will not reject it as degenerate.
SYNTHETIC_SIZE = (320, 240)
MAX_PROBE_IMAGE_BYTES = 10 * 1024 * 1024


def synthetic_test_image(width: int = SYNTHETIC_SIZE[0], height: int = SYNTHETIC_SIZE[1]) -> bytes:
    """A decodable PNG of an empty scene: blue upper half, grey-green lower half.

    Generated from nothing, so it contains no household content. An honest
    model should report no people, vehicles, animals or packages.
    """

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    sky = b"\x87\xb5\xe0" * width
    ground = b"\x6b\x7a\x5e" * width
    raw = b"".join(b"\x00" + (sky if y < height // 2 else ground) for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def load_probe_image(path: Path) -> tuple[bytes, str]:
    """Read an operator-supplied image, refusing anything that is not PNG/JPEG."""
    if path.stat().st_size > MAX_PROBE_IMAGE_BYTES:
        raise ValueError(f"image larger than {MAX_PROBE_IMAGE_BYTES} bytes")
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data, "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return data, "image/jpeg"
    raise ValueError("image must be a PNG or JPEG file")


def probe_context(settings: Settings, camera_key: str | None) -> CameraEventContext:
    """A context for the probe: generic, or a configured camera's own.

    Passing a camera key sends that camera's prompt (including any
    vision_context) with the probe frame -- never one of its images.
    """
    if camera_key is None:
        return CameraEventContext(
            camera_key="probe",
            camera_name="Probe Camera",
            event_type="camera.probe",
            occurred_at=now_utc(),
        )
    camera = load_cameras_config(settings.config_dir).cameras.get(camera_key)
    if camera is None:
        raise ValueError(f"unknown camera key {camera_key!r}")
    return CameraEventContext(
        camera_key=camera_key,
        camera_name=camera.name,
        zone_name=camera.location,
        observes=camera.observes,
        event_type="camera.probe",
        occurred_at=now_utc(),
        vision_context=camera.vision_context,
    )


async def run_probe(
    provider: VisionProvider,
    image: bytes,
    media_type: str,
    context: CameraEventContext,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Analyze once and return a printable summary. No retry, no persistence."""
    result = await provider.analyze(
        VisionRequest(
            image=image, media_type=media_type, context=context, timeout_seconds=timeout_seconds
        )
    )
    return {
        "status": result.status.value,
        "provider": result.provider,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "error_code": result.error_code.value if result.error_code else None,
        "error_message": result.error_message,
        "retryable": result.retryable,
        "observation": result.observation.to_stored_json() if result.observation else None,
    }
