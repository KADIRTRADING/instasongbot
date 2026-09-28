"""Shared pytest fixtures.

DB tests run against an in-memory SQLite database (via aiosqlite) rather than
a mocked session — this exercises the real SQLAlchemy Core/ORM query layer,
just on a different backend than production Postgres. See ARCHITECTURE.md §13
for why this trade-off is acceptable for the repository layer specifically.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base

# Ensure Settings() can be constructed by any test that imports app.config,
# without requiring a real .env file to exist in the test environment.
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with sessionmaker() as session:
        yield session

    await engine.dispose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
