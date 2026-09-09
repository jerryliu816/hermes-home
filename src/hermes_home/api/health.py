"""Liveness and readiness.

The two answer different questions and must not be conflated.

``/health`` — *is this process alive?* Cheap, no dependencies. This is what the
container healthcheck uses. It deliberately does **not** call Home Assistant or
the vision provider: reporting ourselves unhealthy because a dependency is down
would make Docker restart a service that is working correctly, and the whole
point of the durable inbox is that we keep accepting events while dependencies
are unavailable.

``/ready`` — *can this process actually do its job?* Checks the database, the
migration state, and the worker. Use it after a deploy, not as a restart signal.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text

from hermes_home.api.deps import AppState, get_state
from hermes_home.core.time import now_utc
from hermes_home.storage.engine import session_scope
from hermes_home.storage.repositories import DeliveryRepository

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(state: Annotated[AppState, Depends(get_state)]) -> dict[str, Any]:
    """Liveness plus a little operational colour. Never fails on a dependency."""
    queue: dict[str, int] = {}
    database_ok = True
    try:
        async with session_scope(state.session_factory) as session:
            queue = await DeliveryRepository(session).count_by_status()
    except Exception:
        database_ok = False

    return {
        "status": "ok",
        "time": now_utc().isoformat(),
        "version": "0.1.0",
        "database": "ok" if database_ok else "error",
        "vision_provider": state.settings.vision_provider,
        "vision_model": (
            state.settings.vision_model if state.settings.vision_provider != "mock" else None
        ),
        "cameras": sorted(state.cameras.cameras),
        "queue": queue,
    }


@router.get("/ready")
async def ready(
    response: Response, state: Annotated[AppState, Depends(get_state)]
) -> dict[str, Any]:
    """Readiness. Returns 503 when any component would prevent correct operation."""
    checks: dict[str, Any] = {}

    # Database reachable, and actually the schema we expect.
    try:
        async with session_scope(state.session_factory) as session:
            await session.execute(text("SELECT 1"))
            revision = await session.scalar(text("SELECT version_num FROM alembic_version"))
        checks["database"] = {"ok": True}
        checks["migrations"] = {
            "ok": revision == state.expected_revision or state.expected_revision is None,
            "current": revision,
            "expected": state.expected_revision,
        }
    except Exception as exc:
        checks["database"] = {"ok": False, "error": type(exc).__name__}
        checks["migrations"] = {"ok": False, "current": None, "expected": state.expected_revision}

    # The worker is what drains the durable inbox; without it events queue forever.
    worker = state.worker
    checks["worker"] = {
        "ok": worker is not None and worker.running,
        "concurrency": state.settings.ingest_worker_concurrency,
    }

    # The health monitor, when enabled. Deliberately only "is the task alive" --
    # never HA reachability and never a camera's status. An unplugged camera or
    # a rebooting Home Assistant would otherwise make us unready and, through
    # the container healthcheck, drive a restart loop over exactly the condition
    # this service is designed to keep running through.
    if state.settings.camera_health_enabled:
        monitor = state.health_monitor
        checks["camera_health"] = {
            "ok": monitor is not None and monitor.running,
            "interval_seconds": state.settings.camera_health_interval_seconds,
        }

    if state.settings.delivery_reconciliation_enabled:
        reconciler = state.reconciler
        checks["delivery_reconciliation"] = {
            "ok": reconciler is not None and reconciler.running,
            "interval_seconds": state.settings.delivery_reconciliation_interval_seconds,
        }

    # MCP is how Hermes reads any of this.
    checks["mcp"] = {"ok": state.mcp_mounted, "path": "/mcp"}

    checks["config"] = {"ok": bool(state.cameras.cameras), "cameras": len(state.cameras.cameras)}

    ok = all(check.get("ok") for check in checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {"ready": ok, "time": now_utc().isoformat(), "checks": checks}
