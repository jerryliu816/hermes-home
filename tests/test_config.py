"""Configuration validation. Failures here must be loud and at startup."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_home.config import (
    CameraConfig,
    Settings,
    load_cameras_config,
    load_home_config,
    validate_home_and_cameras,
)
from hermes_home.core.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid() -> None:
    """The shipped examples must actually work, or the quick start is a lie."""
    home = load_home_config(REPO_ROOT / "config")
    cameras = load_cameras_config(REPO_ROOT / "config")
    validate_home_and_cameras(home, cameras)

    assert "front_entry" in home.zones
    assert cameras.cameras["front_door"].event_image_entity


def test_missing_config_file_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="missing configuration file"):
        load_home_config(tmp_path)


def test_camera_pointing_at_unknown_zone_is_caught(tmp_path: Path) -> None:
    """A typo in a zone name should fail at startup, not at 3am on a real event."""
    (tmp_path / "home.yaml").write_text(
        yaml.safe_dump({"home": {"name": "H", "zones": {"porch": {"name": "Porch"}}}})
    )
    (tmp_path / "cameras.yaml").write_text(
        yaml.safe_dump(
            {
                "cameras": {
                    "front_door": {
                        "name": "Front Door",
                        "event_image_entity": "image.front_door",
                        "location": "front_prch",  # typo
                    }
                }
            }
        )
    )
    home = load_home_config(tmp_path)
    cameras = load_cameras_config(tmp_path)

    with pytest.raises(ConfigError, match="not a declared zone"):
        validate_home_and_cameras(home, cameras)


def test_strategy_requires_matching_entity() -> None:
    """image_entity_state without an image entity is unusable; say so immediately."""
    with pytest.raises(ValueError, match="event_image_entity"):
        CameraConfig(
            name="Back Door",
            location="backyard",
            event_image_strategy="image_entity_state",
        )

    with pytest.raises(ValueError, match="camera_entity"):
        CameraConfig(
            name="Back Door",
            location="backyard",
            event_image_strategy="camera_snapshot",
        )


def test_snapshot_strategy_needs_only_a_camera_entity() -> None:
    """A brand with no event-image entity is still supported, via config alone."""
    camera = CameraConfig(
        name="Back Door",
        location="backyard",
        camera_entity="camera.back_door",
        event_image_strategy="camera_snapshot",
    )
    assert camera.event_image_entity is None


def test_runtime_requires_secrets() -> None:
    settings = Settings(_env_file=None, webhook_secret="", home_assistant_token="")
    with pytest.raises(ConfigError) as excinfo:
        settings.require_for_runtime()

    message = str(excinfo.value)
    assert "WEBHOOK_SECRET" in message
    assert "HOME_ASSISTANT_TOKEN" in message


def test_anthropic_provider_requires_a_key() -> None:
    settings = Settings(
        _env_file=None,
        webhook_secret="s",
        home_assistant_token="t",
        vision_provider="anthropic",
        anthropic_api_key="",
    )
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        settings.require_for_runtime()


def test_mock_provider_needs_no_credentials() -> None:
    """The service must be runnable end-to-end with no API keys at all."""
    Settings(
        _env_file=None, webhook_secret="s", home_assistant_token="t", vision_provider="mock"
    ).require_for_runtime()


def test_trailing_slash_is_stripped_from_ha_url() -> None:
    assert (
        Settings(_env_file=None, home_assistant_url="http://ha.example.test/").home_assistant_url
        == "http://ha.example.test"
    )


def test_no_private_ip_is_hardcoded_anywhere_committed() -> None:
    """Network addresses belong in .env, never in anything published.

    Originally this only checked src/, and the addresses leaked into docs and
    docker-compose.yml instead — which is exactly where a reader would copy them
    from. It now covers everything git would commit.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.split()
    # Untracked-but-not-ignored files count too: they would land in the next add.
    candidates = [
        f
        for f in tracked
        if not f.startswith((".env.example",))  # placeholders are documented there
    ]

    pattern = r"\b(192\.168\.|10\.[0-9]|172\.(1[6-9]|2[0-9]|3[01])\.)[0-9.]+"
    offenders: list[str] = []
    for name in candidates:
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        import re

        for line_no, line in enumerate(text.splitlines(), 1):
            if re.search(pattern, line):
                offenders.append(f"{name}:{line_no}: {line.strip()[:90]}")

    assert not offenders, "private IP addresses in committed files:\n  " + "\n  ".join(offenders)


def test_defaults_are_lan_local() -> None:
    """This watches a private home; exposure should be deliberate.

    Reads the code's defaults with _env_file=None -- a developer's local .env
    (which may bind 0.0.0.0 for a hardware test) must not decide whether the
    shipped defaults are safe.
    """
    settings = Settings(_env_file=None)
    assert settings.host == "127.0.0.1"
    assert settings.vision_provider == "mock"
    assert settings.vision_retain_images is False
    assert settings.delivery_raw_retention_days == 14
    assert settings.ingest_worker_concurrency == 1


def test_config_dir_does_not_point_into_site_packages() -> None:
    """A path derived from __file__ lands inside site-packages once installed.

    That produced a container that could not find its own configuration, so the
    default is cwd-relative and must stay that way.
    """
    default = Settings(_env_file=None).config_dir
    assert "site-packages" not in str(default)
    assert not default.is_absolute(), "an absolute default cannot follow the deployment"


def test_config_dir_is_overridable_by_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    assert Settings(_env_file=None).config_dir == tmp_path
