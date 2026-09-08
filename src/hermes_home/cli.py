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
        validate_home_and_cameras,
    )

    settings = Settings()
    settings.require_for_runtime()
    home = load_home_config(settings.config_dir)
    cameras = load_cameras_config(settings.config_dir)
    validate_home_and_cameras(home, cameras)
    typer.echo(
        f"configuration ok: {len(home.zones)} zones, {len(cameras.cameras)} cameras, "
        f"vision provider {settings.vision_provider}"
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


if __name__ == "__main__":
    app()
