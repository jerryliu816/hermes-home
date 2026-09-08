"""Alembic environment.

``render_as_batch=True`` is mandatory here. SQLite cannot ALTER a column, drop a
column, or drop a constraint; Alembic's batch mode implements the twelve-step
table-rebuild that actually works, and it depends on the stable constraint names
that ``Base.metadata``'s naming convention supplies.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from hermes_home.storage.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the target database.

    Order matters: an explicit -x db_url wins, then whatever the caller set on
    the Alembic config (how the CLI and the test fixtures pass it), then the
    environment. Without the config step, programmatic callers are silently
    ignored and migrate the wrong database.
    """
    return (
        context.get_x_argument(as_dictionary=True).get("db_url")
        or config.get_main_option("sqlalchemy.url", None)
        or os.environ.get("DATABASE_URL")
        or "sqlite+aiosqlite:///./data/hermes-home.db"
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
