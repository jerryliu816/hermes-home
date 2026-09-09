"""Shared application state and FastAPI dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CamerasConfig, HomeConfig, Settings
from hermes_home.vision.base import VisionProvider

if TYPE_CHECKING:
    from hermes_home.health.monitor import CameraHealthMonitor
    from hermes_home.ingest.worker import IngestWorker


@dataclass
class AppState:
    """Everything built once at startup and shared for the process lifetime."""

    settings: Settings
    home: HomeConfig
    cameras: CamerasConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker
    ha_client: HomeAssistantClient
    vision: VisionProvider
    worker: IngestWorker | None = None
    #: Runs independently of the ingest worker: health must keep being observed
    #: while ingestion is idle, and a failure in one must not affect the other.
    health_monitor: CameraHealthMonitor | None = None
    #: Alembic revision the running code expects; readiness compares the
    #: database against it so a half-applied upgrade is visible rather than
    #: silently serving against the wrong schema.
    expected_revision: str | None = None
    mcp_mounted: bool = False


def get_state(request: Request) -> AppState:
    return request.app.state.app_state
