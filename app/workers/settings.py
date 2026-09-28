"""arq WorkerSettings: process entrypoint for `arq app.workers.settings.WorkerSettings`.

Builds every shared resource (Bot, DB sessionmaker, Redis, recognition
provider, download manager, media tools, storage backend) exactly once in
`on_startup` and stashes it in `ctx["worker_ctx"]` for every task to reuse —
see app/workers/context.py and app/workers/tasks.py.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db.session import dispose_engine, init_engine
from app.logging_conf import configure_logging, get_logger
from app.services.downloader.manager import DownloadManager
from app.services.media.ffmpeg_tools import MediaTools
from app.services.media.tempfiles import cleanup_stale_dirs
from app.services.recognition.factory import get_recognition_provider
from app.services.search.factory import get_search_provider
from app.services.storage.factory import get_storage_backend
from app.workers.context import WorkerContext
from app.workers.tasks import (
    auto_download_job,
    broadcast_job,
    convert_job,
    download_job,
    recognize_job,
    search_deliver_job,
    search_job,
)

logger = get_logger(__name__)


async def on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)

    engine = init_engine(settings)
    sessionmaker: async_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        base_url=settings.telegram_api_url_override,
    )
    redis = Redis.from_url(settings.REDIS_URL)

    ctx["worker_ctx"] = WorkerContext(
        settings=settings,
        bot=bot,
        sessionmaker=sessionmaker,
        redis=redis,
        recognition_provider=get_recognition_provider(settings),
        download_manager=DownloadManager(settings),
        media_tools=MediaTools(
            ffmpeg_binary=settings.FFMPEG_BINARY,
            ffprobe_binary=settings.FFPROBE_BINARY,
            timeout_seconds=settings.FFMPEG_TIMEOUT_SECONDS,
        ),
        storage_backend=get_storage_backend(settings),
        search_provider=get_search_provider(settings),
    )
    logger.info("worker_started")


async def on_shutdown(ctx: dict[str, Any]) -> None:
    worker_ctx: WorkerContext | None = ctx.get("worker_ctx")
    if worker_ctx is not None:
        await worker_ctx.bot.session.close()
        await worker_ctx.redis.aclose()
    await dispose_engine()
    logger.info("worker_stopped")


async def cleanup_temp_files_cron(ctx: dict[str, Any]) -> None:
    """Periodic backstop sweep — see app/services/media/tempfiles.py and
    ARCHITECTURE.md §12. Runs hourly (see cron_jobs below).
    """
    worker_ctx: WorkerContext = ctx["worker_ctx"]
    removed = cleanup_stale_dirs(worker_ctx.settings.WORKDIR, worker_ctx.settings.TEMP_FILE_MAX_AGE_MINUTES)
    if removed:
        logger.info("cleanup_temp_files_cron", removed=removed)


async def prune_old_jobs_cron(ctx: dict[str, Any]) -> None:
    """Daily data-retention sweep — see ARCHITECTURE.md §12 (JOBS_RETENTION_DAYS)."""
    from app.db.repositories import JobRepository

    worker_ctx: WorkerContext = ctx["worker_ctx"]
    async with worker_ctx.sessionmaker() as session:
        deleted = await JobRepository.delete_older_than(session, worker_ctx.settings.JOBS_RETENTION_DAYS)
        await session.commit()
    if deleted:
        logger.info("prune_old_jobs_cron", deleted=deleted)


def _redis_settings_from_url(redis_url: str) -> RedisSettings:
    return RedisSettings.from_dsn(redis_url)


class WorkerSettings:
    functions = [
        recognize_job,
        auto_download_job,
        download_job,
        convert_job,
        broadcast_job,
        search_job,
        search_deliver_job,
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = _redis_settings_from_url(get_settings().REDIS_URL)

    # Bound worker resource usage so a burst of requests can't exhaust the
    # box's memory/CPU/network all at once (see ARCHITECTURE.md §10, "basic
    # abuse prevention ... job monitoring").
    max_jobs = 10
    job_timeout = 600  # 10 minutes; individual services enforce their own tighter timeouts too

    cron_jobs = [
        cron(cleanup_temp_files_cron, hour=None, minute=0),  # top of every hour
        cron(prune_old_jobs_cron, hour=3, minute=0),  # once daily at 03:00
    ]
