"""Database engine and session management.

The PRAGMAs below are not optional decoration:

``foreign_keys=ON``   SQLite disables foreign keys by default, which makes every
                      ``ondelete="CASCADE"`` in the schema purely decorative.
``journal_mode=WAL``  Lets the ingest worker write while a reader (MCP query)
                      reads. Without it, this design would deadlock itself.
``busy_timeout``      Waits instead of raising the instant two writers overlap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def _apply_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_engine(database_url: str) -> AsyncEngine:
    """Build the async engine, creating the SQLite parent directory if needed."""
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        db_path = Path(database_url.split("///", 1)[-1])
        db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(database_url, echo=False, future=True)
    event.listens_for(engine.sync_engine, "connect")(_apply_sqlite_pragmas)
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A transactional session: commit on success, roll back on any exception."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def head_revision(alembic_ini: str) -> str | None:
    """The revision this code expects, read from the migration scripts."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        return ScriptDirectory.from_config(Config(alembic_ini)).get_current_head()
    except Exception:
        return None


async def current_revision(engine: AsyncEngine) -> str | None:
    """The Alembic revision the database is actually at, or None if unmigrated."""
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        except Exception:
            return None
        row = result.first()
        return row[0] if row else None
