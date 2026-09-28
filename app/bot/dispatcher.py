"""Dispatcher factory: the single place that wires together every router and
middleware in the correct order. Both entrypoints (app/main.py for long
polling, app/webhook_app.py for webhook mode) call `build_dispatcher()`
rather than constructing a `Dispatcher` themselves, so the two entrypoints
can never accidentally drift into different middleware/router configurations.

Router order is NOT arbitrary — see the docstring on `_include_routers`
below for a routing-order bug this ordering specifically avoids (caught via
a live Dispatcher test during development, not theoretical).

Middleware order is also deliberate:
  Outer (run for every Update, before aiogram resolves a specific handler):
    1. LoggingMiddleware      - binds update_id/user_id to every log line
       emitted below it, including by a handler or a later middleware.
    2. DbSessionMiddleware    - opens the one session `data["session"]` used
       by everything downstream (repositories, other middlewares).
    3. UserContextMiddleware  - needs `data["session"]` (must run after #2);
       loads/creates the user row, injects `translator`/`is_admin`, enforces
       bans. Everything below this point can assume `db_user`/`translator`
       exist.
    4. ArqPoolMiddleware      - simple static injection, no ordering
       constraint, listed last among outer middlewares for clarity only.
  Inner (run only once aiogram has matched a specific handler — see
  app/bot/middlewares/throttling.py's docstring for exactly why this
  middleware in particular CANNOT be outer):
    5. ThrottlingMiddleware   - needs `data["session"]`/`data["translator"]`
       (from #2/#3, already present since inner runs after outer) and
       `get_flag(data, "rate_limit")`, which only resolves at this level.

aiogram's own FSM middleware (`dp.fsm`, auto-registered as an outer
middleware by the base `Dispatcher.__init__`) runs BEFORE any
user-registered outer middleware (aiogram wires it in its constructor, ahead
of anything `build_dispatcher` adds afterwards) — that's what makes
`state: FSMContext` available to handlers without us doing anything extra
here; see app/bot/states.py and handlers/admin.py for how it's used.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from arq.connections import ArqRedis

from app.bot.error_handler import handle_unexpected_error
from app.bot.handlers import admin, convert, core, download, recognize
from app.bot.middlewares.arq_pool import ArqPoolMiddleware
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.logging import LoggingMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.config import Settings
from app.services.ratelimit.limiter import RateLimiter


def _include_routers(dp: Dispatcher) -> None:
    """Registration order matters here, independent of aiogram's usual
    "first handler to match wins" rule within a single router: aiogram tries
    included routers in registration order and stops at the first one that
    handles the update (see aiogram's Router._propagate_event, which breaks
    out of its `for router in self.sub_routers` loop on the first non-UNHANDLED
    response) — so a broader, un-gated matcher registered before a narrower,
    gated one can steal updates meant for the narrower one.

    Concretely: admin_router's FSM states (waiting for a caption template, a
    button URL, a broadcast message, ...) accept ANY plain-text message while
    active, with no restriction on its content. download_router separately
    matches ANY text message containing a recognized-platform URL, with no
    state check of its own. If download_router were registered first, an
    admin mid-flow who pastes a YouTube/Instagram/... URL as, say, a button's
    destination or part of a broadcast announcement would have that message
    silently hijacked into a probe_job instead of being saved to their actual
    flow — confirmed by a live Dispatcher test that reproduced exactly this
    with the routers in the wrong order (AttributeError deep in
    handlers/download.py, from a callback_query-shaped codepath receiving a
    plain message) before being fixed by this ordering.

    admin_router's own internal gating (`is_admin` router-level filter) means
    registering it first costs nothing for non-admins: their updates simply
    fall through to core/recognize/download/convert exactly as before.
    """
    dp.include_router(admin.router)
    dp.include_router(core.router)
    dp.include_router(recognize.router)
    dp.include_router(convert.router)
    dp.include_router(download.router)


def _include_middlewares(dp: Dispatcher, settings: Settings, rate_limiter: RateLimiter, arq_pool: ArqRedis) -> None:
    # Outer: run for every Update, in this order (see module docstring).
    dp.update.outer_middleware(LoggingMiddleware())
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    dp.update.outer_middleware(ArqPoolMiddleware(arq_pool))

    # Inner: MUST be message/callback_query-level, not update-level -- see
    # ThrottlingMiddleware's own docstring for why this specific one cannot
    # be outer (get_flag() only resolves post-handler-matching).
    throttler = ThrottlingMiddleware(rate_limiter, settings)
    dp.message.middleware(throttler)
    dp.callback_query.middleware(throttler)


def build_dispatcher(
    settings: Settings,
    *,
    arq_pool: ArqRedis,
    rate_limiter: RateLimiter,
    storage: BaseStorage | None = None,
) -> Dispatcher:
    """Build a fully-wired Dispatcher. Both entrypoints call this exactly
    once at startup.

    `storage` defaults to aiogram's in-memory `MemoryStorage`, used by tests
    (see tests/test_dispatcher.py) since it needs no setup. Both real
    entrypoints (app/main.py, app/webhook_app.py) explicitly pass an
    `aiogram.fsm.storage.redis.RedisStorage` built from the same
    `settings.REDIS_URL` already used for the job queue/rate limiter/caches —
    Redis is already a hard dependency of this project (arq cannot run
    without it), so there is no real deployment topology where skipping it
    for FSM storage would make sense, and using it means admin FSM flows
    (mid-broadcast, mid-caption-edit, ...) survive a bot process restart and
    would be shareable across multiple bot replicas rather than pinned to
    whichever process happened to receive the first message in the flow.
    """
    dp = Dispatcher(storage=storage or MemoryStorage())

    dp["settings"] = settings

    _include_middlewares(dp, settings, rate_limiter, arq_pool)
    _include_routers(dp)

    dp.errors.register(handle_unexpected_error)

    return dp


def build_bot(settings: Settings) -> Bot:
    """Construct the aiogram `Bot` instance shared by whichever entrypoint
    calls this (long polling or webhook) — HTML parse mode matches what
    every handler's i18n string and caption template already assumes (see
    app/i18n/locales/*.json and app/services/captions/renderer.py, both of
    which emit `<b>`/`<a href=...>` markup), and `base_url` is only
    overridden when operating a self-hosted Local Bot API Server (see
    Settings.TELEGRAM_API_BASE_URL / ARCHITECTURE.md §9).
    """
    return Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        base_url=settings.telegram_api_url_override,
    )
