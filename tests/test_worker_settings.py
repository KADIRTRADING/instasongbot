"""Tests for app/workers/settings.py: on_startup/on_shutdown resource
lifecycle and the two cron job functions.

`Redis.from_url()` and `Bot(...)` construction are both lazy (no network I/O
happens until a request is actually made — confirmed by every other test
file in this suite that constructs a real `Bot`/`Redis` client against a
fake token/URL without issue), so `on_startup` can run for real against a
real in-memory SQLite engine; only its own `Bot.session.close()`/
`redis.aclose()` calls in `on_shutdown` need no real network to succeed.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.workers.context import WorkerContext
from app.workers.settings import (
    cleanup_temp_files_cron,
    on_shutdown,
    on_startup,
    prune_old_jobs_cron,
)


@pytest.fixture(autouse=True)
async def reset_db_engine():
    await db_session_module.dispose_engine()
    yield
    await db_session_module.dispose_engine()


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        REDIS_URL="redis://localhost:6379/0",
        RECOGNITION_PROVIDER="audd",
        AUDD_API_TOKEN="test",
        STORAGE_BACKEND="local",
        PUBLIC_BASE_URL="http://localhost:8080",
        WORKDIR=str(tmp_path),
    )
    # on_startup calls the module-level get_settings() (an lru_cache'd
    # singleton), not the `settings` fixture value directly -- patch the
    # cache so on_startup actually builds its WorkerContext from THIS
    # fixture's Settings instance instead of whatever .env/env-vars
    # get_settings() would otherwise resolve.
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: s)
    import app.workers.settings as worker_settings_module

    monkeypatch.setattr(worker_settings_module, "get_settings", lambda: s)
    return s


async def test_on_startup_populates_worker_ctx_with_all_resources(settings: Settings) -> None:
    ctx: dict = {}
    await on_startup(ctx)

    assert "worker_ctx" in ctx
    worker_ctx = ctx["worker_ctx"]
    assert isinstance(worker_ctx, WorkerContext)
    assert worker_ctx.settings is settings
    assert worker_ctx.bot is not None
    assert worker_ctx.sessionmaker is not None
    assert worker_ctx.redis is not None
    assert worker_ctx.recognition_provider is not None
    assert worker_ctx.download_manager is not None
    assert worker_ctx.media_tools is not None
    assert worker_ctx.storage_backend is not None

    await on_shutdown(ctx)


async def test_on_startup_initializes_db_engine_usable_by_sessionmaker(settings: Settings) -> None:
    """on_startup's sessionmaker must be bound to a real, usable engine --
    exercised here by creating the schema and running one real query through
    the exact sessionmaker on_startup constructed."""
    ctx: dict = {}
    await on_startup(ctx)
    worker_ctx = ctx["worker_ctx"]

    engine = db_session_module.get_sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.db.repositories import UserRepository

    async with worker_ctx.sessionmaker() as session:
        user = await UserRepository.get_or_create(
            session, user_id=1, username=None, first_name=None, default_language="en"
        )
        await session.commit()
        assert user.id == 1

    await on_shutdown(ctx)


async def test_on_shutdown_closes_bot_session_and_redis_and_disposes_engine(settings: Settings) -> None:
    ctx: dict = {}
    await on_startup(ctx)
    worker_ctx = ctx["worker_ctx"]

    # The Bot's underlying aiohttp session is created lazily on first HTTP
    # request (see aiogram's AiohttpSession.create_session()) -- force it
    # into existence here so on_shutdown's `bot.session.close()` call has a
    # real, open session to actually close, making the assertion below
    # meaningful rather than trivially true on a session that was never opened.
    aiohttp_session = await worker_ctx.bot.session.create_session()
    assert not aiohttp_session.closed

    await on_shutdown(ctx)

    assert aiohttp_session.closed

    # The global DB engine must also have been disposed (get_sessionmaker
    # now raises, per app/db/session.py's contract).
    with pytest.raises(RuntimeError, match="not initialized"):
        db_session_module.get_sessionmaker()


async def test_on_shutdown_with_no_worker_ctx_does_not_raise() -> None:
    """arq calls on_shutdown even if on_startup never ran/failed -- must not
    crash on a ctx dict with no "worker_ctx" key."""
    await on_shutdown({})  # must not raise

    with pytest.raises(RuntimeError, match="not initialized"):
        db_session_module.get_sessionmaker()


# --- cleanup_temp_files_cron -------------------------------------------------


async def test_cleanup_temp_files_cron_removes_stale_dirs(settings: Settings, tmp_path) -> None:
    import time

    tmp_root = tmp_path / "tmp"
    stale_dir = tmp_root / "old-job-id"
    stale_dir.mkdir(parents=True)
    old_time = time.time() - (settings.TEMP_FILE_MAX_AGE_MINUTES + 10) * 60
    import os

    os.utime(stale_dir, (old_time, old_time))

    fresh_dir = tmp_root / "fresh-job-id"
    fresh_dir.mkdir(parents=True)

    ctx: dict = {}
    await on_startup(ctx)

    await cleanup_temp_files_cron(ctx)

    assert not stale_dir.exists()
    assert fresh_dir.exists()

    await on_shutdown(ctx)


# --- prune_old_jobs_cron ------------------------------------------------


async def test_prune_old_jobs_cron_deletes_old_job_rows(settings: Settings) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.db.models import Job
    from app.db.repositories import JobRepository, UserRepository

    ctx: dict = {}
    await on_startup(ctx)
    worker_ctx = ctx["worker_ctx"]

    engine = db_session_module.get_sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with worker_ctx.sessionmaker() as session:
        await UserRepository.get_or_create(session, user_id=1, username=None, first_name=None, default_language="en")
        await JobRepository.create(session, job_id="old-job", user_id=1, job_type="download")
        await JobRepository.create(session, job_id="new-job", user_id=1, job_type="download")
        await session.commit()

        # Backdate "old-job"'s created_at past the retention window so the
        # cron's cutoff query actually matches it.
        old_time = datetime.now(UTC) - timedelta(days=settings.JOBS_RETENTION_DAYS + 5)
        await session.execute(update(Job).where(Job.id == "old-job").values(created_at=old_time))
        await session.commit()

    await prune_old_jobs_cron(ctx)

    async with worker_ctx.sessionmaker() as session:
        assert await JobRepository.get(session, "old-job") is None
        assert await JobRepository.get(session, "new-job") is not None

    await on_shutdown(ctx)


async def test_prune_old_jobs_cron_with_nothing_to_prune_does_not_raise(settings: Settings) -> None:
    ctx: dict = {}
    await on_startup(ctx)

    engine = db_session_module.get_sessionmaker().kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await prune_old_jobs_cron(ctx)  # must not raise on an empty jobs table

    await on_shutdown(ctx)
