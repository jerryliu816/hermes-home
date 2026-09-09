"""Database engine and session management.

The PRAGMAs below are not optional decoration:

``foreign_keys=ON``   SQLite disables foreign keys by default, which makes every
                      ``ondelete="CASCADE"`` in the schema purely decorative.
``journal_mode=WAL``  Lets the ingest worker write while a reader (MCP query)
                      reads. Without it, this design would deadlock itself.
``busy_timeout``      Waits instead of raising the instant two writers overlap.
``synchronous=FULL``  fsync the WAL on every commit rather than only at
                      checkpoints. Measured cost here is ~0.06ms per commit for
                      a handful of events a day, so the cheaper NORMAL buys
                      nothing worth the weaker guarantee.

A caveat that must not be glossed: on a macOS Docker bind mount, fsync is
largely advisory. Measured on this deployment, FULL costs 1.3x OFF on the bind
mount but 92.7x OFF on the container's own filesystem -- the virtiofs layer
acknowledges the sync without durably flushing to the host disk. So FULL is set
because it is correct and free, not because it makes power-loss durability
guaranteed here. The guarantee we can actually verify is cross-connection
visibility, which is why the webhook confirms every delivery through a fresh
connection before answering 202.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

logger = structlog.get_logger(__name__)


def _apply_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=FULL")
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


def create_verification_engine(database_url: str) -> AsyncEngine:
    """A pool-less engine used only to re-read what we just wrote.

    ``NullPool`` is the entire point: every connect() opens a brand-new SQLite
    connection with its own file handle and its own view of the WAL index. A
    session borrowed from the ordinary pool may well be handed back the very
    connection that performed the write, which would confirm nothing -- a write
    is always visible to the connection that made it.

    Opening a SQLite connection costs well under a millisecond, and this runs
    once per inbound webhook, so the price is irrelevant next to what it buys:
    proof that the row is visible to a reader that was not party to the commit.
    """
    engine = create_async_engine(database_url, echo=False, future=True, poolclass=NullPool)
    event.listens_for(engine.sync_engine, "connect")(_apply_sqlite_pragmas)
    return engine


async def database_identity(engine: AsyncEngine) -> dict[str, object]:
    """Which file, exactly, is this engine writing to?

    Recorded because "the database" is an assumption, not an observation. A
    process can hold an open handle to a file that has been renamed or replaced
    underneath it and keep writing happily into something nothing else will ever
    read. Device and inode are what distinguish that from the healthy case, and
    they cost one stat call at startup.
    """
    info: dict[str, object] = {}
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text("PRAGMA database_list"))).all()
            for _seq, name, file in rows:
                if name == "main":
                    info["path"] = file or ":memory:"
            info["journal_mode"] = await conn.scalar(text("PRAGMA journal_mode"))
            info["synchronous"] = await conn.scalar(text("PRAGMA synchronous"))
        path = info.get("path")
        if isinstance(path, str) and path and path != ":memory:":
            st = os.stat(path)
            info["device"] = st.st_dev
            info["inode"] = st.st_ino
            info["size_bytes"] = st.st_size
            try:
                info["wal_bytes"] = os.stat(path + "-wal").st_size
            except OSError:
                info["wal_bytes"] = 0
    except Exception as exc:  # identity is diagnostic, never load-bearing
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


async def confirm_delivery_durable(engine: AsyncEngine, delivery_uid: str) -> bool:
    """Can a connection that did not write this row actually see it?

    This is the check that would have caught the 2026-09-09 incident, in which
    commits returned success while rows never became visible: four webhook
    deliveries were acknowledged to Home Assistant with 202 and then simply did
    not exist. Home Assistant had no way to know, so the events were lost
    silently rather than surfacing as a failure it could report.

    Returns False rather than raising on a database error: an unreachable or
    broken database is precisely the condition being tested for, and the caller
    turns either outcome into the same honest 5xx.
    """
    try:
        async with engine.connect() as conn:
            found = await conn.scalar(
                text("SELECT 1 FROM event_deliveries WHERE uid = :uid"),
                {"uid": delivery_uid},
            )
        return found is not None
    except Exception:
        logger.exception("durability.verify_failed", delivery_uid=delivery_uid)
        return False


async def quick_check(engine: AsyncEngine) -> tuple[bool, str]:
    """``PRAGMA quick_check``: does the database still look structurally sound?

    Read-only and reporting-only. Nothing here repairs, rebuilds, vacuums or
    deletes: a corrupt database is a situation for a human and a backup, and an
    automatic repair would destroy the evidence of what went wrong.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.scalar(text("PRAGMA quick_check"))
        answer = str(result)
        return answer == "ok", answer
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


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
