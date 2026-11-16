"""Async SQLite engine and session factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Settings, get_settings
from .models import Base

log = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _configure_sqlite(dbapi_connection, _record) -> None:
    """WAL plus a busy timeout: the worker writes while the UI polls."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.close()


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = settings or get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(settings.db_url, echo=False, future=True)
        event.listen(_engine.sync_engine, "connect", _configure_sqlite)
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(settings), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Transactional session; commits on success, rolls back on error."""
    factory = get_sessionmaker(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _literal_default(column) -> str | None:
    """A SQL literal for a column's Python-side default, if it has a scalar one.

    SQLite cannot add a NOT NULL column without a default, and the defaults
    here are declared Python-side, so they have to be rendered by hand.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\'", "\'\'")
        return f"\'{escaped}\'"
    return None


def _add_missing_columns(conn) -> None:
    """Bring an existing database up to the current model definitions.

    ``create_all`` creates missing tables but never alters existing ones, so a
    new column would otherwise break every install that already has a
    database - and the database holds the connected accounts, which are not
    something to ask someone to set up again. SQLite can add a column in
    place, which covers every schema change made so far.
    """
    inspector = sa_inspect(conn)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all will build it complete
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            coltype = column.type.compile(conn.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {coltype}'
            literal = _literal_default(column)
            if not column.nullable:
                if literal is None:
                    # Better a nullable column than a database that will not
                    # open; the model's own default still applies on insert.
                    log.warning(
                        "adding %s.%s as nullable: no default to backfill with",
                        table.name, column.name,
                    )
                else:
                    ddl += f" NOT NULL DEFAULT {literal}"
            elif literal is not None:
                ddl += f" DEFAULT {literal}"
            log.info("migrating database: %s", ddl)
            conn.exec_driver_sql(ddl)


async def init_db(settings: Settings | None = None) -> None:
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
    log.info("database ready at %s", (settings or get_settings()).db_path)


async def dispose_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def healthcheck() -> bool:
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:  # pragma: no cover - diagnostics
        return False
