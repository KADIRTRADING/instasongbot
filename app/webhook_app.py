"""Webhook entrypoint: `python -m app.webhook_app`.

Runs one aiohttp `Application` that serves BOTH the Telegram webhook route
(`WEBHOOK_PATH`, default `/webhook`) and, when `STORAGE_BACKEND=local`, the
signed large-file download route (`/files/{filename}`, see
app/file_server.py) — one process, one port, sitting behind a reverse proxy
that terminates TLS (see the bundled Caddy config referenced in the README
and ARCHITECTURE.md §11).

Compared to long polling (app/main.py), this mode needs a public HTTPS URL
and an inbound port open, but scales the bot process itself horizontally
better (each incoming webhook POST is one independent HTTP request, not a
single shared long-poll loop) and matches the deployment shape most PaaS/
container platforms expect. `WEBHOOK_SECRET` is verified on every request
via aiogram's own `SimpleRequestHandler` (constant-time comparison against
the `X-Telegram-Bot-Api-Secret-Token` header) BEFORE the update ever reaches
the dispatcher — an unset secret is allowed only because Telegram itself
lets an operator register a webhook without one, but operators are strongly
encouraged to set `WEBHOOK_SECRET` (see README) since without it, anyone who
learns the webhook URL could POST fabricated updates to the bot.

Like app/main.py, this module contains no business logic — it builds shared
resources and hands them to app/bot/dispatcher.py's `build_dispatcher()`.
"""

from __future__ import annotations

from aiogram.fsm.storage.redis import RedisStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from arq import create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.bot.dispatcher import build_bot, build_dispatcher
from app.config import Settings, get_settings
from app.db.seed import seed_defaults
from app.db.session import dispose_engine, get_sessionmaker, init_engine
from app.file_server import register_file_routes
from app.logging_conf import configure_logging, get_logger

logger = get_logger(__name__)

BOT_KEY = web.AppKey("instasongbot_bot", object)


async def create_webhook_app(settings: Settings | None = None) -> web.Application:
    """Build the full aiohttp Application. Factored out from `main()` (the
    process entrypoint below) so tests can build and exercise the exact same
    app via aiohttp's test client without going through `web.run_app`'s
    blocking event loop takeover — see tests/test_webhook_app.py.
    """
    settings = settings or get_settings()
    configure_logging(settings)

    if not settings.WEBHOOK_BASE_URL:
        raise RuntimeError(
            "WEBHOOK_BASE_URL is required in webhook mode (used to register the webhook "
            "with Telegram) -- set it or use app.main (long polling) instead. See README."
        )

    init_engine(settings)
    async with get_sessionmaker()() as session:
        await seed_defaults(session)

    fsm_redis = Redis.from_url(settings.REDIS_URL)
    rate_limit_redis = Redis.from_url(settings.REDIS_URL)
    arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))

    from app.services.ratelimit.limiter import RateLimiter

    bot = build_bot(settings)
    dp = build_dispatcher(
        settings,
        arq_pool=arq_pool,
        rate_limiter=RateLimiter(rate_limit_redis),
        storage=RedisStorage(fsm_redis),
    )

    app = web.Application()
    app[BOT_KEY] = bot

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.WEBHOOK_SECRET or None)
    webhook_handler.register(app, path=settings.WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    if settings.STORAGE_BACKEND == "local":
        register_file_routes(app, settings)

    async def _on_startup(app: web.Application) -> None:
        webhook_url = settings.WEBHOOK_BASE_URL.rstrip("/") + settings.WEBHOOK_PATH
        await bot.set_webhook(
            url=webhook_url,
            secret_token=settings.WEBHOOK_SECRET or None,
            drop_pending_updates=False,
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("webhook_registered", url=webhook_url)

    async def _on_cleanup(app: web.Application) -> None:
        logger.info("webhook_app_stopping")
        await arq_pool.aclose()
        await fsm_redis.aclose()
        await rate_limit_redis.aclose()
        await dispose_engine()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    return app


def main() -> None:
    settings = get_settings()
    web.run_app(create_webhook_app(settings), host=settings.WEB_SERVER_HOST, port=settings.WEB_SERVER_PORT)


if __name__ == "__main__":
    main()
