"""Application assembly.

Small on purpose: it wires components together and owns the process lifecycle.
The routers hold the HTTP surface, the pipeline holds the logic, and neither
knows about the other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hermes_home.api import health, webhooks
from hermes_home.api.deps import AppState
from hermes_home.clients.home_assistant import HomeAssistantClient
from hermes_home.config import (
    Settings,
    load_cameras_config,
    load_home_config,
    validate_home_and_cameras,
)
from hermes_home.health.monitor import CameraHealthMonitor
from hermes_home.health.reconcile import DeliveryReconciler
from hermes_home.ingest.worker import IngestWorker
from hermes_home.observability.logging import configure_logging
from hermes_home.spatial import seed_home
from hermes_home.storage.engine import (
    create_engine,
    create_session_factory,
    create_verification_engine,
    current_revision,
    head_revision,
    quick_check,
    session_scope,
)
from hermes_home.vision.registry import build_provider

logger = structlog.get_logger(__name__)


async def build_state(settings: Settings, *, start_worker: bool = True) -> AppState:
    home = load_home_config(settings.config_dir)
    cameras = load_cameras_config(settings.config_dir)
    validate_home_and_cameras(home, cameras)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    verify_engine = create_verification_engine(settings.database_url)

    # Refuse to serve against a schema this code was not written for. Starting
    # anyway means writing rows a later upgrade cannot interpret, and a loud
    # failure now is far cheaper than that.
    revision = await current_revision(engine)
    expected = head_revision(str(settings.alembic_ini))
    if revision is None:
        raise RuntimeError("database has no schema; run 'hermes-home db upgrade' before starting")
    if expected is not None and revision != expected:
        raise RuntimeError(
            f"database is at migration {revision} but this code expects {expected}; "
            "run 'hermes-home db upgrade' (the deploy entrypoint does this for you)"
        )

    async with session_scope(session_factory) as session:
        await seed_home(session, home, cameras)

    state = AppState(
        settings=settings,
        home=home,
        cameras=cameras,
        engine=engine,
        session_factory=session_factory,
        ha_client=HomeAssistantClient(
            settings.home_assistant_url,
            settings.home_assistant_token,
            timeout_seconds=settings.home_assistant_timeout_seconds,
        ),
        vision=build_provider(settings),
        verify_engine=verify_engine,
        expected_revision=expected,
    )

    # Structural sanity, reported and never repaired. A corrupt database is a
    # situation for a human and a backup; repairing automatically would destroy
    # the evidence of what went wrong.
    state.integrity_ok, state.integrity_detail = await quick_check(engine)
    if not state.integrity_ok:
        logger.error("database.quick_check_failed", detail=state.integrity_detail)
    

    if start_worker:
        state.worker = IngestWorker(
            session_factory=session_factory,
            settings=settings,
            cameras=cameras,
            ha_client=state.ha_client,
            vision=state.vision,
        )
        state.health_monitor = CameraHealthMonitor(
            session_factory=session_factory,
            settings=settings,
            cameras=cameras,
            ha_client=state.ha_client,
        )
        # A third independent task. Camera health and delivery reconciliation
        # answer different questions and fail independently -- on 2026-09-09
        # every camera was healthy while four deliveries were lost -- so
        # neither is allowed to depend on the other.
        state.reconciler = DeliveryReconciler(
            session_factory=session_factory,
            settings=settings,
            cameras=cameras,
            ha_client=state.ha_client,
        )
    return state


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = await build_state(settings)
        app.state.app_state = state
        if state.worker is not None:
            await state.worker.start()
        if state.health_monitor is not None:
            await state.health_monitor.start()
        if state.reconciler is not None:
            await state.reconciler.start()

        state.mcp_mounted = bool(getattr(app.state, "mcp_mounted", False))
        mcp = getattr(app.state, "mcp_server", None)
        logger.info(
            "service.started",
            vision_provider=settings.vision_provider,
            cameras=sorted(state.cameras.cameras),
            mcp_enabled=mcp is not None,
        )
        try:
            if mcp is None:
                yield
            else:
                # A mounted sub-application's own lifespan never runs, so the
                # session manager has to be entered here or every MCP request
                # fails with "Task group is not initialized".
                async with mcp.session_manager.run():
                    yield
        finally:
            if state.reconciler is not None:
                await state.reconciler.stop()
            if state.health_monitor is not None:
                await state.health_monitor.stop()
            if state.worker is not None:
                await state.worker.stop()
            await state.ha_client.aclose()
            if state.verify_engine is not None:
                await state.verify_engine.dispose()
            await state.engine.dispose()

    app = FastAPI(
        title="hermes-home",
        version="0.1.0",
        summary="Semantic event memory bridging Home Assistant and the Hermes Agent",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Reject oversized bodies before they are parsed."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit():
            if int(declared) > settings.webhook_max_body_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "request body too large"},
                )
        return await call_next(request)

    app.include_router(health.router)
    app.include_router(webhooks.router)
    _mount_mcp(app, settings)
    return app


def _mount_mcp(app: FastAPI, settings: Settings) -> None:
    """Mount the MCP server, if the optional dependency is installed.

    Kept optional so the webhook pipeline still runs in an environment without
    the `mcp` extra; a missing package degrades the MCP surface, not ingestion.
    """
    try:
        from hermes_home.mcp.server import (
            MCP_PATH,
            build_transport_security,
            create_mcp_server,
        )
    except ImportError:  # pragma: no cover - depends on the optional extra
        logger.warning("mcp.unavailable", hint="pip install 'hermes-home[mcp]'")
        return

    # The tools close over app.state.app_state, which the lifespan populates
    # before any request is served.
    class _LazyState:
        def __getattr__(self, item: str) -> object:
            return getattr(app.state.app_state, item)

    mcp = create_mcp_server(_LazyState())  # type: ignore[arg-type]
    app.state.mcp_server = mcp
    app.state.mcp_mounted = True
    app.mount(
        MCP_PATH,
        mcp.streamable_http_app(
            streamable_http_path="/",
            transport_security=build_transport_security(settings),
        ),
    )
    logger.info("mcp.mounted", path=MCP_PATH, allowed_hosts=settings.mcp_host_allowlist())
