"""The opt-in live probe: its frame, its guards, and that it stays offline here.

The probe itself is a live network call by design; these tests only exercise
it over a mock transport and check the CLI refuses the calls it should.
"""

from __future__ import annotations

import json
import struct
import zlib

import httpx
import pytest
from typer.testing import CliRunner

from hermes_home.cli import app
from hermes_home.config import Settings
from hermes_home.vision.ollama import OllamaVisionProvider
from hermes_home.vision.probe import (
    load_probe_image,
    probe_context,
    run_probe,
    synthetic_test_image,
)
from tests.conftest import FIXTURE_CONFIG


def test_synthetic_frame_is_a_decodable_png() -> None:
    data = synthetic_test_image()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (320, 240)
    # Decompress the pixel data to prove the IDAT stream is well formed.
    idat_len = struct.unpack(">I", data[33:37])[0]
    assert data[37:41] == b"IDAT"
    raw = zlib.decompress(data[41 : 41 + idat_len])
    assert len(raw) == height * (1 + width * 3)


def test_probe_image_must_be_png_or_jpeg(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_bytes(b"not an image")
    with pytest.raises(ValueError):
        load_probe_image(path)
    png = tmp_path / "frame.png"
    png.write_bytes(synthetic_test_image())
    assert load_probe_image(png)[1] == "image/png"


def test_probe_context_uses_only_the_named_camera_s_prompt() -> None:
    settings = Settings(_env_file=None, config_dir=FIXTURE_CONFIG)
    assert probe_context(settings, None).vision_context is None
    assert probe_context(settings, "gecko").vision_context
    assert probe_context(settings, "front_door").vision_context is None
    with pytest.raises(ValueError):
        probe_context(settings, "no_such_camera")


async def test_run_probe_reports_the_validated_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={
                "model": "gemma4:31b",
                "message": {"content": json.dumps({"scene_summary": "Empty scene."})},
                "done": True,
                "done_reason": "stop",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OllamaVisionProvider(
        base_url="http://ollama.test", model="gemma4:31b", client=client
    )
    settings = Settings(_env_file=None, config_dir=FIXTURE_CONFIG)
    summary = await run_probe(
        provider,
        synthetic_test_image(),
        "image/png",
        probe_context(settings, None),
        timeout_seconds=5,
    )
    assert summary["status"] == "ok"
    assert summary["provider"] == "ollama"
    assert summary["observation"]["person_count"] is None
    await client.aclose()


@pytest.mark.parametrize(
    ("env", "needle"),
    [
        ({"VISION_PROVIDER": "mock"}, "nothing to probe"),
        ({"VISION_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"}, "--allow-billed"),
    ],
)
def test_probe_refuses_mock_and_unconfirmed_billed_providers(
    env, needle, tmp_path, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here, so nothing real is read
    result = CliRunner().invoke(app, ["vision", "probe"], env=env)
    assert result.exit_code == 2
    assert needle in result.output


def test_check_contacts_nothing_for_non_ollama_providers(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["vision", "check"], env={"VISION_PROVIDER": "mock"})
    assert result.exit_code == 0
    assert "nothing was contacted" in result.output
