"""Command line interface."""

from __future__ import annotations

import asyncio

import typer

from hermes_home.config import Settings

app = typer.Typer(
    help="hermes-home: semantic event memory for Home Assistant.", no_args_is_help=True
)
db_app = typer.Typer(help="Database maintenance.", no_args_is_help=True)
app.add_typer(db_app, name="db")
vision_app = typer.Typer(
    help="Vision provider diagnostics. Never touches the database.", no_args_is_help=True
)
app.add_typer(vision_app, name="vision")
sensors_app = typer.Typer(
    help="Sensor readings: what is synced, and what HA has that is not configured.",
    no_args_is_help=True,
)
app.add_typer(sensors_app, name="sensors")
thermostat_app = typer.Typer(
    help="Thermostats configured in config/sensors.yaml.", no_args_is_help=True
)
app.add_typer(thermostat_app, name="thermostat")


def _alembic_config(settings: Settings):  # type: ignore[no-untyped-def]
    from alembic.config import Config

    config = Config(str(settings.alembic_ini))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    config.attributes["configure_logger"] = False
    return config


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply migrations."""
    import os

    from alembic import command

    settings = Settings()
    os.environ.setdefault("DATABASE_URL", settings.database_url)
    command.upgrade(_alembic_config(settings), revision)
    typer.echo(f"database upgraded to {revision}")


@db_app.command("prune")
def db_prune() -> None:
    """Null out raw webhook bodies past the retention horizon."""
    from hermes_home.ingest.retention import prune_raw_bodies
    from hermes_home.storage.engine import create_engine, create_session_factory

    settings = Settings()

    async def _run() -> int:
        engine = create_engine(settings.database_url)
        try:
            return await prune_raw_bodies(
                create_session_factory(engine),
                retention_days=settings.delivery_raw_retention_days,
            )
        finally:
            await engine.dispose()

    typer.echo(f"pruned {asyncio.run(_run())} raw bodies")


@app.command("seed-zones")
def seed_zones() -> None:
    """Load config/home.yaml and config/cameras.yaml into the database."""
    from hermes_home.config import (
        load_cameras_config,
        load_home_config,
        validate_home_and_cameras,
    )
    from hermes_home.spatial import seed_home
    from hermes_home.storage.engine import (
        create_engine,
        create_session_factory,
        session_scope,
    )

    settings = Settings()
    home = load_home_config(settings.config_dir)
    cameras = load_cameras_config(settings.config_dir)
    validate_home_and_cameras(home, cameras)

    async def _run() -> int:
        engine = create_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            async with session_scope(factory) as session:
                zone_ids = await seed_home(session, home, cameras)
            return len(zone_ids)
        finally:
            await engine.dispose()

    typer.echo(f"seeded {asyncio.run(_run())} zones and {len(cameras.cameras)} cameras")


@app.command("check-config")
def check_config() -> None:
    """Validate environment and YAML configuration, then exit."""
    from hermes_home.config import (
        load_cameras_config,
        load_home_config,
        load_sensors_config,
        validate_cameras_against_settings,
        validate_home_and_cameras,
        validate_sensors,
    )

    settings = Settings()
    settings.require_for_runtime()
    home = load_home_config(settings.config_dir)
    cameras = load_cameras_config(settings.config_dir)
    validate_home_and_cameras(home, cameras)
    validate_cameras_against_settings(settings, cameras)
    sensors = load_sensors_config(settings.config_dir)
    validate_sensors(home, sensors, cameras)
    typer.echo(
        f"configuration ok: {len(home.zones)} zones, {len(cameras.cameras)} cameras, "
        f"{len(sensors.sensors)} sensors, vision provider {settings.vision_provider}"
    )


@app.command("serve")
def serve() -> None:
    """Run the API and the ingest worker."""
    import uvicorn

    settings = Settings()
    settings.require_for_runtime()
    uvicorn.run(
        "hermes_home.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


@vision_app.command("check")
def vision_check() -> None:
    """Show what the configured provider reports about its model. Metadata only."""
    import json

    from hermes_home.vision.registry import build_provider

    settings = Settings()
    typer.echo(f"provider: {settings.vision_provider}")
    if settings.vision_provider != "ollama":
        typer.echo("no metadata check for this provider; nothing was contacted")
        return
    provider = build_provider(settings)

    async def _run() -> dict:
        try:
            return await provider.diagnose()  # type: ignore[attr-defined]
        finally:
            await provider.aclose()  # type: ignore[attr-defined]

    report = asyncio.run(_run())
    typer.echo(json.dumps(report, indent=2))
    if not (report["model_present"] and report["vision"] and report["think_setting_ok"]):
        typer.echo("NOT USABLE for camera analysis (see report above)", err=True)
        raise typer.Exit(1)
    typer.echo("model present and reports vision capability")


@vision_app.command("probe")
def vision_probe(
    image: str = typer.Option(
        None, help="PNG/JPEG to send instead of the generated synthetic frame."
    ),
    camera: str = typer.Option(
        None, help="Use this camera's prompt (and vision_context); never its images."
    ),
    allow_billed: bool = typer.Option(
        False, "--allow-billed", help="Required to probe a billed cloud provider."
    ),
) -> None:
    """Run ONE live analysis through the real provider code. No DB, no files written."""
    import json
    from pathlib import Path

    from hermes_home.vision.probe import (
        load_probe_image,
        probe_context,
        run_probe,
        synthetic_test_image,
    )
    from hermes_home.vision.registry import build_provider

    settings = Settings()
    if settings.vision_provider == "mock":
        typer.echo("VISION_PROVIDER=mock makes no network call; nothing to probe", err=True)
        raise typer.Exit(2)
    if settings.vision_provider == "anthropic" and not allow_billed:
        typer.echo("anthropic is billed per call; pass --allow-billed to probe it", err=True)
        raise typer.Exit(2)

    if image:
        data, media_type = load_probe_image(Path(image))
        source = "operator-supplied file"
    else:
        data, media_type = synthetic_test_image(), "image/png"
        source = "generated synthetic frame (no household content)"
    context = probe_context(settings, camera)

    typer.echo(
        f"LIVE NETWORK CALL: {settings.vision_provider} model {settings.vision_model}, "
        f"image: {source}, prompt: {'camera ' + camera if camera else 'generic'}, "
        f"deadline {settings.vision_timeout_seconds:g}s"
    )
    provider = build_provider(settings)

    async def _run() -> dict:
        try:
            return await run_probe(
                provider,
                data,
                media_type,
                context,
                timeout_seconds=settings.vision_timeout_seconds,
            )
        finally:
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()

    summary = asyncio.run(_run())
    typer.echo(json.dumps(summary, indent=2))
    if summary["status"] != "ok":
        raise typer.Exit(1)


@sensors_app.command("status")
def sensors_status() -> None:
    """Latest stored reading per measurement, and how far each is synced.

    Reads the database, so run it inside the container (make sensors-status).
    """
    from hermes_home.config import load_sensors_config
    from hermes_home.services.sensor_service import SensorService
    from hermes_home.storage.engine import create_engine, create_session_factory, session_scope

    settings = Settings()
    sensors = load_sensors_config(settings.config_dir)
    if not sensors.sensors:
        typer.echo("no sensors configured (config/sensors.yaml is absent or empty)")
        return

    async def _run() -> list[dict]:
        engine = create_engine(settings.database_url)
        try:
            async with session_scope(create_session_factory(engine)) as session:
                svc = SensorService(session, settings=settings, sensors=sensors)
                return await svc.current(sorted(sensors.sensors))
        finally:
            await engine.dispose()

    for sensor in asyncio.run(_run()):
        kind = "thermostat, " if sensor["kind"] == "thermostat" else ""
        typer.echo(f"{sensor['key']}  ({kind}{sensor['name']}, zone {sensor['zone']})")
        for name, m in sensor["measurements"].items():
            value = "-" if m["value"] is None else f"{m['value']} {m['unit'] or ''}".strip()
            flag = {True: "in range", False: "OUT OF RANGE", None: ""}[m["in_range"]]
            typer.echo(
                f"  {name:<12} {value:<12} {m['status']:<12} {flag:<13} "
                f"confirmed {m['confirmed_through'] or 'never'}"
                + (f"  error {m['sync_error']}" if m["sync_error"] else "")
            )


@sensors_app.command("discover")
def sensors_discover(
    all_numeric: bool = typer.Option(
        False, "--all-numeric", help="Offer every numeric sensor, not just temp/humidity/battery."
    ),
) -> None:
    """List Home Assistant sensor entities, and print a stub for unconfigured ones.

    Read-only: never writes configuration.
    """
    from hermes_home.clients.home_assistant import HomeAssistantClient
    from hermes_home.config import load_sensors_config
    from hermes_home.sensors.discover import candidates, stub, thermostat_stub

    settings = Settings()
    sensors = load_sensors_config(settings.config_dir)

    async def _run():  # type: ignore[no-untyped-def]
        async with HomeAssistantClient(
            settings.home_assistant_url,
            settings.home_assistant_token,
            timeout_seconds=settings.home_assistant_timeout_seconds,
        ) as client:
            return await client.list_states()

    states = asyncio.run(_run())
    found = candidates(states, sensors, all_numeric=all_numeric)
    climate = thermostat_stub(states, sensors)
    if climate:
        typer.echo("# Thermostats not configured yet; merge into the sensor for that room:\n")
        typer.echo(climate + "\n")
    if not found:
        typer.echo("no matching sensor entities in Home Assistant")
        return
    for c in found:
        where = f"configured as {c.configured_as}" if c.configured_as else "not configured"
        reading = f"{c.state} {c.unit or ''}".strip()
        typer.echo(f"{c.entity_id:<55} {reading:<12} {where}")
    text = stub(found)
    if text:
        typer.echo("\n# Paste under `sensors:` in config/sensors.yaml, then make restart:\n")
        typer.echo(text)


@thermostat_app.command("status")
def thermostat_status() -> None:
    """Each thermostat's live state from Home Assistant, and its recent changes.

    Read-only. Run it inside the container (make thermostat-status).
    """
    from hermes_home.clients.home_assistant import HomeAssistantClient
    from hermes_home.config import load_sensors_config
    from hermes_home.services.thermostat_service import ThermostatService
    from hermes_home.storage.engine import create_engine, create_session_factory

    settings = Settings()
    sensors = load_sensors_config(settings.config_dir)
    if not sensors.thermostats():
        typer.echo("no thermostat configured (a sensor with a thermostat: block)")
        return

    async def _run() -> list[dict]:
        engine = create_engine(settings.database_url)
        try:
            async with HomeAssistantClient(
                settings.home_assistant_url,
                settings.home_assistant_token,
                timeout_seconds=settings.home_assistant_timeout_seconds,
            ) as client:
                svc = ThermostatService(
                    session_factory=create_session_factory(engine),
                    settings=settings,
                    sensors=sensors,
                    ha_client=client,
                )
                return [await svc.status(k) for k in sorted(sensors.thermostats())]
        finally:
            await engine.dispose()

    for t in asyncio.run(_run()):
        limits = t["limits"]
        typer.echo(
            f"{t['key']}  ({t['name']}, {t['entity']}, zone {t['zone']})  "
            f"limits {limits['setpoint_min']:g}-{limits['setpoint_max']:g} {limits['unit']}, "
            f"modes {', '.join(limits['modes'])}"
        )
        now = t.get("now")
        if now is None:
            typer.echo(
                f"  status {t['status']}" + (f"  error {t['error']}" if t.get("error") else "")
            )
        else:
            target = (
                f"{now['setpoint']:g}"
                if now["setpoint"] is not None
                else f"{now['setpoint_low']}-{now['setpoint_high']}"
                if now["setpoint_low"] is not None
                else "-"
            )
            typer.echo(
                f"  mode {now['mode']}  action {now['action']}  setpoint {target}  "
                f"preset {now['preset']}  now {now['current_temperature']}"
                f" / {now['current_humidity']}%"
            )
        for c in t["recent_changes"]:
            typer.echo(
                f"  {c['requested_at']}  {c['outcome']:<11} {c['request']}"
                + (f"  {c['error_code']}" if c["error_code"] else "")
            )


if __name__ == "__main__":
    app()
