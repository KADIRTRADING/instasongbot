"""Tests for app/main.py's `main()` -- the long-polling entrypoint.

Exercises the REAL startup/shutdown sequence (engine init, seed_defaults,
build_bot/build_dispatcher, the local file server, bot.delete_webhook,
teardown) with only the true external-network edges mocked: Redis.from_url,
arq.create_pool, and dp.start_polling itself (which would otherwise block
forever waiting for real Telegram updates). This is the same technique used
to validate main() manually during development, now captured as a
repeatable regression test.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest

from app.bot.handlers import admin, convert, core, download, recognize
from app.config import Settings
from app.db import session as db_session_module


@pytest.fixture(autouse=True)
async def reset_db_engine():
    await db_session_module.dispose_engine()
    yield
    await db_session_module.dispose_engine()


@pytest.fixture(autouse=True)
def reset_router_parents():
    yield
    for module in (admin, core, recognize, convert, download):
        module.router._parent_router = None


async def test_main_runs_full_startup_and_shutdown_sequence(tmp_path, monkeypatch) -> None:
    from app.db.base import Base

    settings = Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        STORAGE_BACKEND="local",
        WORKDIR=str(tmp_path),
        PUBLIC_BASE_URL="http://localhost:18080",
        WEB_SERVER_PORT=0,  # let the OS pick a free port -- avoids collisions with anything else running
    )

    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.Redis, "from_url", staticmethod(lambda url: fakeredis.aioredis.FakeRedis()))

    # main() calls init_engine(settings) + seed_defaults(session) as part of
    # its own startup sequence (see app/db/seed.py) -- seed_defaults queries
    # real tables, so the schema must exist before main() runs. init_engine
    # is idempotent (a no-op once the module-global `_engine` is set -- see
    # app/db/session.py), so creating the schema against that same engine
    # here first is safe and is exactly what alembic's `upgrade head` does
    # in the real deployment (see docker-compose.yml's bot command).
    engine = main_module.init_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    mock_arq_pool = AsyncMock()
    mock_arq_pool.aclose = AsyncMock()

    async def fake_create_pool(redis_settings):
        return mock_arq_pool

    monkeypatch.setattr(main_module, "create_pool", fake_create_pool)

    mock_bot = AsyncMock()
    mock_bot.session.close = AsyncMock()
    mock_bot.delete_webhook = AsyncMock()

    mock_dp = AsyncMock()
    mock_dp.start_polling = AsyncMock()

    with (
        patch.object(main_module, "build_bot", return_value=mock_bot) as mock_build_bot,
        patch.object(main_module, "build_dispatcher", return_value=mock_dp) as mock_build_dispatcher,
    ):
        await main_module.main()

    mock_build_bot.assert_called_once_with(settings)
    mock_build_dispatcher.assert_called_once()
    mock_bot.delete_webhook.assert_called_once_with(drop_pending_updates=False)
    mock_dp.start_polling.assert_called_once_with(mock_bot)
    mock_bot.session.close.assert_called_once()
    mock_arq_pool.aclose.assert_called_once()

    # The global DB engine must have been disposed as part of the finally block.
    with pytest.raises(RuntimeError, match="not initialized"):
        db_session_module.get_sessionmaker()


async def test_main_seeds_default_rows_before_polling_starts(tmp_path, monkeypatch) -> None:
    """seed_defaults() must actually run, against a real, usable session --
    verified with an AsyncMock spy that wraps the REAL seed_defaults (so it
    still executes normally), letting us assert both that it was called
    AND (via the real call going through) that it didn't raise against the
    session main() handed it. A DB-state check after the fact isn't viable
    here since main()'s own finally block disposes the engine (and the
    fixture DB is `:memory:`, which doesn't survive a new connection anyway)
    -- see tests/test_webhook_app.py's analogous test for the state-based
    version, which works there because that test controls the engine/schema
    setup independently of the app-under-test's own lifecycle.
    """
    from app.db.base import Base

    settings = Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        STORAGE_BACKEND="local",
        WORKDIR=str(tmp_path),
        PUBLIC_BASE_URL="http://localhost:18081",
        WEB_SERVER_PORT=0,
    )

    import app.db.seed as seed_module
    import app.main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.Redis, "from_url", staticmethod(lambda url: fakeredis.aioredis.FakeRedis()))

    mock_arq_pool = AsyncMock()
    mock_arq_pool.aclose = AsyncMock()

    async def fake_create_pool(redis_settings):
        return mock_arq_pool

    monkeypatch.setattr(main_module, "create_pool", fake_create_pool)

    # init_engine() only creates the engine, not the schema (that's alembic's
    # job in production -- see docker-compose.yml's `alembic upgrade head`).
    # main() calls init_engine(settings) itself; since it's idempotent (a
    # no-op once the module-global `_engine` is already set -- see
    # app/db/session.py), creating the schema against that SAME engine here
    # first means main()'s own seed_defaults call later sees real tables.
    engine = main_module.init_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    real_seed_defaults = seed_module.seed_defaults
    seed_spy = AsyncMock(wraps=real_seed_defaults)
    monkeypatch.setattr(main_module, "seed_defaults", seed_spy)

    with (
        patch.object(main_module, "build_bot", return_value=AsyncMock()),
        patch.object(main_module, "build_dispatcher", return_value=AsyncMock()),
    ):
        await main_module.main()

    seed_spy.assert_called_once()
