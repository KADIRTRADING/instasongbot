"""Per-action rate limiting, driven by a `@flags.rate_limit("action")` marker
on individual handlers (see app/bot/handlers/*.py) plus an always-on global
burst check for every update. Limits are resolved from `bot_settings` (hot,
admin-editable via the admin panel) falling back to the env-sourced defaults
in Settings — see ARCHITECTURE.md §10.

Denied requests never reach the handler: this middleware answers directly
with a "you're going too fast" message and stops propagation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config import Settings
from app.constants import RateLimitAction
from app.db.repositories import BotSettingRepository
from app.services.ratelimit.limiter import RateLimiter

# Env-sourced fallback limits, keyed by action, used when no admin override
# exists yet in bot_settings. window is always 60s for the per-action limits
# (see Settings.RATE_LIMIT_*_PER_MINUTE); the global burst window is shorter
# to catch rapid-fire spam specifically, not sustained-but-slow usage.
_GLOBAL_BURST_WINDOW_SECONDS = 10


class ThrottlingMiddleware(BaseMiddleware):
    """IMPORTANT: must be registered as an INNER middleware — i.e.
    `dp.message.middleware(...)` / `dp.callback_query.middleware(...)` — NOT
    as an outer middleware on `dp.update`. This is not a style preference:
    `get_flag(data, "rate_limit")` only resolves correctly once aiogram has
    matched the update to a specific handler (which happens between the
    outer and inner middleware layers), so a `@flags.rate_limit(...)`-tagged
    handler's flag is invisible to any outer middleware. This was caught by
    a live end-to-end test that fed real Updates through a real Dispatcher —
    see tests/test_middlewares.py.

    At the inner-middleware level, `event` is the actual Message/CallbackQuery
    (not the wrapping Update), unlike outer middlewares.
    """

    def __init__(self, rate_limiter: RateLimiter, settings: Settings) -> None:
        self._limiter = rate_limiter
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("db_user")
        if user is None:
            return await handler(event, data)

        session = data["session"]
        translator = data["translator"]

        # Global burst check applies to every update, regardless of handler.
        burst_limit = await BotSettingRepository.get(
            session, "rate_limit_global_burst", default=self._settings.RATE_LIMIT_GLOBAL_BURST_PER_MINUTE
        )
        burst_decision = await self._limiter.check(
            user_id=user.id,
            action=RateLimitAction.GLOBAL_BURST.value,
            limit=burst_limit,
            window_seconds=_GLOBAL_BURST_WINDOW_SECONDS,
        )
        if not burst_decision.allowed:
            await _deny(event, translator, burst_decision.retry_after_seconds)
            return None

        # Per-action check only applies to handlers that opted in via the
        # `rate_limit` flag (recognize/download/convert — see handlers).
        action = get_flag(data, "rate_limit")
        if action is not None:
            limit = await BotSettingRepository.get(
                session, f"rate_limit_{action}", default=self._default_for(action)
            )
            decision = await self._limiter.check(user_id=user.id, action=action, limit=limit, window_seconds=60)
            if not decision.allowed:
                await _deny(event, translator, decision.retry_after_seconds)
                return None

        return await handler(event, data)

    def _default_for(self, action: str) -> int:
        return {
            RateLimitAction.RECOGNIZE.value: self._settings.RATE_LIMIT_RECOGNIZE_PER_MINUTE,
            RateLimitAction.DOWNLOAD.value: self._settings.RATE_LIMIT_DOWNLOAD_PER_MINUTE,
            RateLimitAction.CONVERT.value: self._settings.RATE_LIMIT_CONVERT_PER_MINUTE,
        }.get(action, 5)


async def _deny(event: TelegramObject, translator, retry_after_seconds: float) -> None:
    text = translator.t("error_rate_limit_user", seconds=max(1, round(retry_after_seconds)))
    if isinstance(event, Message):
        await event.answer(text)
    elif isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
