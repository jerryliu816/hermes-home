"""Shared application state and FastAPI dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import CamerasConfig, HomeConfig, Settings
from hermes_home.vision.base import VisionProvider

if TYPE_CHECKING:
    from hermes_home.health.monitor import CameraHealthMonitor
    from hermes_home.health.reconcile import DeliveryReconciler
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
    #: Detects Home Assistant events that never reached us. Independent of the
    #: health monitor: the two failures are unrelated and have already occurred
    #: separately.
    reconciler: DeliveryReconciler | None = None
    #: Alembic revision the running code expects; readiness compares the
    #: database against it so a half-applied upgrade is visible rather than
    #: silently serving against the wrong schema.
    #: Pool-less engine used to re-read a just-committed delivery through a
    #: connection that did not write it. Optional so tests can construct state
    #: without one; the webhook falls back to the main engine and says so.
    verify_engine: AsyncEngine | None = None
    #: Result of PRAGMA quick_check at startup. Reported, never acted on.
    #: Absolute path, device and inode of the database actually in use.
    database_identity: dict = field(default_factory=dict)
    integrity_ok: bool = True
    integrity_detail: str = "ok"
    expected_revision: str | None = None
    mcp_mounted: bool = False


def get_state(request: Request) -> AppState:
    return request.app.state.app_state
