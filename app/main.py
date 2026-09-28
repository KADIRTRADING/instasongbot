"""Long-polling entrypoint: `python -m app.main`.

No public URL, no TLS, no inbound ports required from Telegram's side — the
box only needs outbound HTTPS to api.telegram.org (and to whichever
platforms/APIs the worker talks to). This is the recommended default for
"just get it running on a bare AWS EC2/Lightsail instance" (ARCHITECTURE.md
§11): there's no reverse proxy, certificate, or firewall rule to get right
before the bot starts responding.

Runs two things concurrently in this one process:
  1. aiogram's polling loop (`dp.start_polling`), which itself calls
     `Dispatcher.emit_startup`/`emit_shutdown` around the loop — but note
     those hooks are for aiogram-registered `@dp.startup()`/`@dp.shutdown()`
     callbacks, of which this project has none; all of OUR setup (engine,
     Redis, arq pool) happens explicitly before `start_polling` is even
     called, and is torn down in the `finally` block below, so it's
     guaranteed to run exactly once regardless of aiogram's own hook timing.
  2. A small aiohttp server (app/file_server.py) on `WEB_SERVER_HOST:
     WEB_SERVER_PORT`, needed because the local storage backend's "here's a
     temporary download link" feature (ARCHITECTURE.md §9) requires SOME
     HTTP server to serve those bytes from, and long-polling mode has none
     otherwise. Skipped entirely when `STORAGE_BACKEND=s3`, since S3
     presigned URLs are served by AWS, not by us.

This module intentionally contains no business logic of its own — it only
constructs shared resources and wires them into app/bot/dispatcher.py's
`build_dispatcher()`, exactly like app/webhook_app.py and
app/workers/settings.py's `on_startup` do for their own processes. See
ARCHITECTURE.md §2 for why the bot and worker are separate processes
sharing this one codebase.
"""

from __future__ import annotations

import asyncio

from aiogram.fsm.storage.redis import RedisStorage
from aiohttp import web
from arq import create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.bot.dispatcher import build_bot, build_dispatcher
from app.config import get_settings
from app.db.seed import seed_defaults
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.file_server import create_file_server_app
from app.logging_conf import configure_logging, get_logger
from app.services.ratelimit.limiter import RateLimiter

logger = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings)

    init_engine(settings)
    async with get_sessionmaker()() as session:
        await seed_defaults(session)

    fsm_redis = Redis.from_url(settings.REDIS_URL)
    rate_limit_redis = Redis.from_url(settings.REDIS_URL)
    arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))

    bot = build_bot(settings)
    dp = build_dispatcher(
        settings,
        arq_pool=arq_pool,
        rate_limiter=RateLimiter(rate_limit_redis),
        storage=RedisStorage(fsm_redis),
    )

    file_server_runner: web.AppRunner | None = None
    if settings.STORAGE_BACKEND == "local":
        file_app = create_file_server_app(settings)
        file_server_runner = web.AppRunner(file_app)
        await file_server_runner.setup()
        site = web.TCPSite(file_server_runner, settings.WEB_SERVER_HOST, settings.WEB_SERVER_PORT)
        await site.start()
        logger.info("file_server_started", host=settings.WEB_SERVER_HOST, port=settings.WEB_SERVER_PORT)

    logger.info("bot_starting_long_polling")
    try:
        # Long polling and webhook mode are mutually exclusive at Telegram's
        # side (Bot API notes: "You will not be able to receive updates using
        # getUpdates for as long as an outgoing webhook is set up") — drop
        # any webhook left registered from a previous webhook-mode deployment
        # so polling actually receives updates instead of silently getting none.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        logger.info("bot_stopping")
        if file_server_runner is not None:
            await file_server_runner.cleanup()
        await bot.session.close()
        await arq_pool.aclose()
        await fsm_redis.aclose()
        await rate_limit_redis.aclose()
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
