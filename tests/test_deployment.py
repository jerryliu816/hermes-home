"""Deployment behaviour: readiness, migration guarding, graceful shutdown.

These are the properties an always-running service is judged on, so they are
tested rather than assumed.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from hermes_home.api.app import build_state, create_app
from hermes_home.api.deps import AppState
from hermes_home.core.ids import delivery_key
from hermes_home.core.time import now_utc
from hermes_home.health.monitor import CameraHealthMonitor
from hermes_home.health.reconcile import DeliveryReconciler
from hermes_home.ingest.worker import IngestWorker
from hermes_home.storage.engine import create_verification_engine, head_revision, session_scope
from hermes_home.storage.repositories import DeliveryRepository
from hermes_home.vision.mock import MockVisionProvider


@pytest.fixture
async def client(session_factory, settings, home_config, cameras_config, fake_ha):
    app = create_app(settings)
    app.state.app_state = AppState(
        settings=settings,
        home=home_config,
        cameras=cameras_config,
        engine=create_verification_engine(settings.database_url),
        verify_engine=create_verification_engine(settings.database_url),
        session_factory=session_factory,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
        worker=None,
        expected_revision=head_revision("alembic.ini"),
        mcp_mounted=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, app.state.app_state


# --------------------------------------------------------------------------- #
# Health vs readiness
# --------------------------------------------------------------------------- #


async def test_health_stays_ok_when_home_assistant_is_down(client) -> None:
    """A dependency outage must not make Docker restart a working service."""
    http, state = client
    state.ha_client.raise_on_state = RuntimeError("HA unreachable")

    response = await http.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_reports_database_and_queue(client) -> None:
    http, _ = client
    body = (await http.get("/health")).json()

    assert body["database"] == "ok"
    assert "queue" in body
    assert body["version"]


async def test_ready_is_true_when_everything_is_wired(
    client, session_factory, settings, cameras_config, fake_ha
) -> None:
    http, state = client
    state.worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
    )
    state.health_monitor = CameraHealthMonitor(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
    )
    state.reconciler = DeliveryReconciler(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
    )
    await state.worker.start()
    await state.health_monitor.start()
    await state.reconciler.start()
    try:
        response = await http.get("/ready")
        body = response.json()
    finally:
        await state.reconciler.stop()
        await state.health_monitor.stop()
        await state.worker.stop(grace_seconds=1)

    assert response.status_code == 200, body
    assert body["ready"] is True
    assert body["checks"]["database"]["ok"]
    assert body["checks"]["migrations"]["ok"]
    assert body["checks"]["worker"]["ok"]
    assert body["checks"]["mcp"]["ok"]
    assert body["checks"]["camera_health"]["ok"]
    assert body["checks"]["delivery_reconciliation"]["ok"]


async def test_ready_is_503_without_a_worker(client) -> None:
    """Events would queue forever with no worker; that is not ready."""
    http, _ = client
    response = await http.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    assert body["checks"]["worker"]["ok"] is False


async def test_ready_reports_a_migration_mismatch(client) -> None:
    """A half-applied upgrade must be visible, not silently served against."""
    http, state = client
    state.expected_revision = "some-future-revision"

    body = (await http.get("/ready")).json()

    assert body["ready"] is False
    assert body["checks"]["migrations"]["ok"] is False
    assert body["checks"]["migrations"]["expected"] == "some-future-revision"


async def test_ready_is_503_when_the_database_is_gone(client) -> None:
    http, state = client
    await state.session_factory.kw["bind"].dispose()

    class _Broken:
        def __call__(self, *a, **k):
            raise RuntimeError("database gone")

    state.session_factory = _Broken()
    response = await http.get("/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["database"]["ok"] is False


# --------------------------------------------------------------------------- #
# Startup guards
# --------------------------------------------------------------------------- #


async def test_startup_refuses_an_unmigrated_database(settings, tmp_path) -> None:
    """Better to fail loudly than to write rows against an unknown schema."""
    settings = settings.model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"}
    )
    with pytest.raises(RuntimeError, match="no schema"):
        await build_state(settings, start_worker=False)


async def test_startup_refuses_a_schema_from_the_future(settings, engine, monkeypatch) -> None:
    """A database ahead of the code is a rollback that lost data; stop."""
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE alembic_version SET version_num = 'not-our-head'"))

    with pytest.raises(RuntimeError, match="expects"):
        await build_state(settings, start_worker=False)


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #


async def _enqueue(session_factory) -> str:
    moment = now_utc()
    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).enqueue(
            source="home_assistant",
            delivery_key=delivery_key(
                source="home_assistant",
                event_type="camera.person_detected",
                source_entity_id="image.front_door_event_image",
                occurred_at=moment,
            ),
            correlation_id="shutdown-test",
            raw_body={
                "event_type": "camera.person_detected",
                "camera": "front_door",
                "entity_id": "image.front_door_event_image",
                "timestamp": moment.isoformat(),
                "metadata": {},
            },
        )
        return delivery.uid


async def test_shutdown_lets_an_in_flight_job_finish(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """Not required for correctness -- leases cover us -- but it avoids paying
    twice for a vision call that already succeeded."""
    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(latency_seconds=0.3),
    )
    uid = await _enqueue(session_factory)

    await worker.start()
    await asyncio.sleep(0.1)  # let it claim and begin
    await worker.stop(grace_seconds=10)

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        assert delivery.status == "completed", "the in-flight job should have finished"


async def test_shutdown_is_bounded_and_leaves_work_recoverable(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    """A job that overruns the grace period is cancelled, not waited on forever.

    It stays claimable because its lease expires, which is the same mechanism
    that recovers from a hard kill.
    """
    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(latency_seconds=5.0),
    )
    uid = await _enqueue(session_factory)

    await worker.start()
    await asyncio.sleep(0.2)
    await asyncio.wait_for(worker.stop(grace_seconds=0.2), timeout=5)

    assert not worker.running

    async with session_scope(session_factory) as session:
        delivery = await DeliveryRepository(session).get_by_uid(uid)
        # Still claimed, with a lease that will expire and free it again.
        assert delivery.status in ("processing", "pending", "completed")
        assert delivery.attempts >= 1


async def test_stopping_an_idle_worker_is_quick(
    session_factory, settings, cameras_config, fake_ha
) -> None:
    worker = IngestWorker(
        session_factory=session_factory,
        settings=settings,
        cameras=cameras_config,
        ha_client=fake_ha,
        vision=MockVisionProvider(),
    )
    await worker.start()
    await asyncio.wait_for(worker.stop(grace_seconds=5), timeout=3)
    assert not worker.running


def test_alembic_ini_default_does_not_point_into_site_packages() -> None:
    """The migration guard is only useful if it can find the migration scripts.

    A path built from ``__file__`` resolves inside site-packages once installed,
    which silently turned the guard into a no-op in the container: readiness
    reported `expected: null` and served happily against any schema.
    """
    from hermes_home.config import Settings as S

    default = S(_env_file=None).alembic_ini
    assert "site-packages" not in str(default)
    assert not default.is_absolute()


def test_head_revision_is_resolvable_from_the_working_directory() -> None:
    from hermes_home.config import Settings as S

    revision = head_revision(str(S(_env_file=None).alembic_ini))
    assert revision, "head revision must resolve, or the startup guard is inert"


# --------------------------------------------------------------------------- #
# Startup integrity check
# --------------------------------------------------------------------------- #


async def test_quick_check_passes_on_a_healthy_database(settings) -> None:
    from hermes_home.storage.engine import create_engine, quick_check

    engine = create_engine(settings.database_url)
    try:
        ok, detail = await quick_check(engine)
    finally:
        await engine.dispose()
    assert ok is True
    assert detail == "ok"


async def test_quick_check_reports_rather_than_raises_on_a_broken_file(tmp_path) -> None:
    """Corruption is a situation for a human and a backup.

    Nothing here repairs, rebuilds or deletes: an automatic repair would
    destroy the evidence of what went wrong, which is the only thing that makes
    the next occurrence diagnosable.
    """
    from hermes_home.storage.engine import create_engine, quick_check

    broken = tmp_path / "broken.db"
    broken.write_bytes(b"SQLite format 3\x00" + b"\xde\xad\xbe\xef" * 512)
    engine = create_engine(f"sqlite+aiosqlite:///{broken}")
    try:
        ok, detail = await quick_check(engine)
    finally:
        await engine.dispose()

    assert ok is False
    assert detail  # says something about what is wrong
    assert broken.exists(), "the damaged file must be left exactly as found"


async def test_ready_reports_integrity(client) -> None:
    http, _ = client
    body = (await http.get("/ready")).json()
    assert body["checks"]["integrity"]["ok"] is True
    assert body["checks"]["integrity"]["detail"] == "ok"


async def test_a_failed_integrity_check_makes_the_service_unready(client) -> None:
    http, state = client
    state.integrity_ok = False
    state.integrity_detail = "*** in database main *** Page 4 is never used"
    try:
        response = await http.get("/ready")
    finally:
        state.integrity_ok, state.integrity_detail = True, "ok"

    assert response.status_code == 503
    assert response.json()["checks"]["integrity"]["ok"] is False


async def test_liveness_never_fails_on_integrity(client) -> None:
    """The container healthcheck reads /health.

    Restarting a process because its database is corrupt would only corrupt it
    in a loop, so integrity is reported there and never allowed to fail it.
    """
    http, state = client
    state.integrity_ok = False
    state.integrity_detail = "malformed"
    try:
        response = await http.get("/health")
    finally:
        state.integrity_ok, state.integrity_detail = True, "ok"

    assert response.status_code == 200
    assert response.json()["integrity"] == "malformed"
