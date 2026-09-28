"""Async SQLAlchemy engine/session factory.

A single module-level engine is created lazily from Settings so both the bot
process and the worker process (and tests, with a different DATABASE_URL) can
import `get_sessionmaker()` without duplicating engine-construction logic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(settings: Settings) -> AsyncEngine:
    """Create (once) and return the global async engine. Idempotent."""
    global _engine, _sessionmaker
    if _engine is None:
        connect_args: dict[str, object] = {}
        # aiosqlite (used in tests) doesn't understand pool_size/max_overflow.
        engine_kwargs: dict[str, object] = {}
        if settings.DATABASE_URL.startswith("postgresql"):
            engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
            engine_kwargs["max_overflow"] = settings.DB_POOL_SIZE
            engine_kwargs["pool_pre_ping"] = True
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=settings.DB_ECHO,
            connect_args=connect_args,
            **engine_kwargs,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialized. Call init_engine(settings) first.")
    return _sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager that commits on success and rolls back on exception."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
