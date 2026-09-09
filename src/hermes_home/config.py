"""Configuration.

Two sources, deliberately separated:

* **Environment / ``.env``** -- secrets and deployment settings. Never committed.
* **``config/*.yaml``** -- the non-secret description of the home: zones, their
  relationships, and which camera watches what.

Both are validated at startup, and failures are loud: a service that silently
starts with no webhook secret is worse than one that refuses to start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hermes_home.core.errors import ConfigError

#: Where the house description lives, relative to the working directory.
#:
#: Deliberately cwd-relative rather than derived from this file's location: once
#: the package is pip-installed, a path built from ``__file__`` points inside
#: site-packages, which is both wrong and confusing. The working directory is
#: the repo root in development and /app in the container, so both resolve
#: correctly, and CONFIG_DIR overrides it anywhere else.
DEFAULT_CONFIG_DIR = Path("config")

#: Alembic config, resolved the same cwd-relative way and for the same reason.
DEFAULT_ALEMBIC_INI = Path("alembic.ini")


class Settings(BaseSettings):
    """Environment-driven settings. Secrets live here and nowhere else."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Home Assistant. The URL is entirely env-driven so a changed HA address
    # never requires a source change.
    home_assistant_url: str = "http://homeassistant.local"
    home_assistant_token: str = ""
    home_assistant_timeout_seconds: float = 10.0

    # Webhook
    webhook_secret: str = ""
    webhook_max_body_bytes: int = 65536

    # Vision
    vision_provider: Literal["mock", "anthropic"] = "mock"
    vision_model: str = "claude-sonnet-5"
    anthropic_api_key: str = ""
    vision_timeout_seconds: float = 30.0
    vision_retain_images: bool = False

    # Ingest worker
    ingest_worker_concurrency: int = Field(default=1, ge=1, le=16)
    ingest_max_attempts: int = Field(default=5, ge=1, le=20)
    ingest_lease_seconds: int = Field(default=120, ge=10)
    ingest_poll_seconds: float = 1.0
    dedupe_window_seconds: int = Field(default=15, ge=0)
    correlation_window_seconds: int = Field(default=120, ge=0)
    #: How long an incident stays open after its last event. Matches the
    #: correlation window by default: an incident should remain open exactly as
    #: long as it could still legitimately attract another correlated event.
    incident_idle_seconds: int = Field(default=120, ge=0)
    #: How often the maintenance loop looks for incidents to close.
    incident_sweep_interval_seconds: int = Field(default=60, ge=1)

    # Freshness gate. Measured on real Eufy hardware: the event still lands on
    # the image entity ~3.7s after the detection trigger fires, so the budget
    # (attempts x interval) must comfortably exceed that. 15s by default.
    # Waiting is free here -- the webhook was acknowledged long ago.
    freshness_poll_attempts: int = Field(default=15, ge=1, le=120)
    freshness_poll_interval_seconds: float = Field(default=1.0, gt=0, le=10)
    #: How far before the trigger an event still may be stamped and still count
    #: as belonging to it. Both timestamps come from Home Assistant's own clock,
    #: so this only absorbs integrations that stamp at capture, not at receipt.
    freshness_tolerance_seconds: float = Field(default=5.0, ge=0, le=120)

    # Retention
    delivery_raw_retention_days: int = Field(default=14, ge=1)
    retention_sweep_interval_seconds: int = 3600

    # Service. Loopback by default -- this watches a private home, so exposure
    # should be a deliberate act, not a default.
    host: str = "127.0.0.1"
    port: int = 8099
    database_url: str = "sqlite+aiosqlite:///./data/hermes-home.db"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"
    display_timezone: str = "UTC"

    #: How long shutdown waits for an in-flight delivery before cancelling it.
    #: Must be comfortably under the container stop grace period, or Docker
    #: SIGKILLs us mid-job. A job is freshness-wait + vision, so ~30s is typical.
    shutdown_grace_seconds: float = Field(default=30.0, ge=0, le=300)

    #: Extra Host header values the MCP endpoint will accept, comma separated.
    #: DNS-rebinding protection stays ON; this widens the allowlist rather than
    #: disabling it. localhost and 127.0.0.1 (the path Hermes uses, running on
    #: the same machine) are always included. Add a LAN address here only if an
    #: MCP client connects across the network.
    mcp_allowed_hosts: str = ""

    home_name: str = "Home"
    config_dir: Path = DEFAULT_CONFIG_DIR
    alembic_ini: Path = DEFAULT_ALEMBIC_INI

    @field_validator("home_assistant_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    def mcp_host_allowlist(self) -> list[str]:
        """Host values the MCP endpoint accepts, with and without the port."""
        names = ["localhost", "127.0.0.1"]
        names += [h.strip() for h in self.mcp_allowed_hosts.split(",") if h.strip()]
        allowed: list[str] = []
        for name in names:
            host = name.split(":", 1)[0]
            for candidate in (host, f"{host}:{self.port}", name):
                if candidate not in allowed:
                    allowed.append(candidate)
        return allowed

    def require_for_runtime(self) -> None:
        """Fail fast on configuration that is required to actually serve traffic.

        Not called by the test suite, which supplies its own settings.
        """
        missing: list[str] = []
        if not self.webhook_secret:
            missing.append("WEBHOOK_SECRET")
        if not self.home_assistant_token:
            missing.append("HOME_ASSISTANT_TOKEN")
        if self.vision_provider == "anthropic" and not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY (required when VISION_PROVIDER=anthropic)")
        if missing:
            raise ConfigError(
                "missing required configuration: " + ", ".join(missing) + ". See .env.example."
            )


# --------------------------------------------------------------------------- #
# Home description (non-secret YAML)
# --------------------------------------------------------------------------- #


class ZoneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["interior", "exterior", "threshold", "structure"] = "exterior"
    parent: str | None = None
    # Optional geometry: normalized coordinates, polygons, orientation. Absent in
    # v1; present in the model so a house map needs no schema change later.
    attributes: dict[str, Any] = Field(default_factory=dict)


class ZoneRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_zone: str = Field(alias="from")
    to_zone: str = Field(alias="to")
    relation: Literal["adjacent", "leads_to", "overlooks"] = "adjacent"
    bidirectional: bool = True


class HomeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Home"
    zones: dict[str, ZoneConfig] = Field(default_factory=dict)
    relationships: list[ZoneRelation] = Field(default_factory=list)


class CameraConfig(BaseModel):
    """How to obtain one camera's event still, and where it looks.

    ``event_image_strategy`` is the seam that keeps vendor differences out of the
    pipeline. Eufy exposes an ``image.*`` entity whose state advances on each new
    event, which gives us a freshness signal; other cameras may not. Swapping
    strategy is a config change, not a code change.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    camera_entity: str | None = None
    event_image_entity: str | None = None
    event_image_strategy: Literal["image_entity_state", "camera_snapshot", "none"] = (
        "image_entity_state"
    )
    location: str
    observes: list[str] = Field(default_factory=list)

    @field_validator("event_image_strategy")
    @classmethod
    def _strategy_has_entity(cls, value: str, info: Any) -> str:
        return value

    def model_post_init(self, _context: Any) -> None:
        if self.event_image_strategy == "image_entity_state" and not self.event_image_entity:
            raise ValueError(
                f"camera {self.name!r} uses strategy 'image_entity_state' "
                "but has no event_image_entity"
            )
        if self.event_image_strategy == "camera_snapshot" and not self.camera_entity:
            raise ValueError(
                f"camera {self.name!r} uses strategy 'camera_snapshot' but has no camera_entity"
            )


class CamerasConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cameras: dict[str, CameraConfig] = Field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing configuration file: {path} (copy the .example alongside it)")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_home_config(config_dir: Path) -> HomeConfig:
    raw = _load_yaml(config_dir / "home.yaml")
    return HomeConfig.model_validate(raw.get("home", raw))


def load_cameras_config(config_dir: Path) -> CamerasConfig:
    return CamerasConfig.model_validate(_load_yaml(config_dir / "cameras.yaml"))


def validate_home_and_cameras(home: HomeConfig, cameras: CamerasConfig) -> None:
    """Cross-check the two files. A camera pointing at a zone that does not exist
    is a typo we should catch at startup, not at 3am on a real event."""
    known = set(home.zones)
    problems: list[str] = []
    for key, camera in cameras.cameras.items():
        if camera.location not in known:
            problems.append(f"camera {key!r} location {camera.location!r} is not a declared zone")
        for observed in camera.observes:
            if observed not in known:
                problems.append(f"camera {key!r} observes unknown zone {observed!r}")
    for rel in home.relationships:
        for endpoint in (rel.from_zone, rel.to_zone):
            if endpoint not in known:
                problems.append(f"relationship references unknown zone {endpoint!r}")
    if problems:
        raise ConfigError("invalid home configuration:\n  - " + "\n  - ".join(problems))
