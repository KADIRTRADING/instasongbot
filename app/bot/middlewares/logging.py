"""Structured request logging: one log line per update, with user/chat/update
type bound via structlog contextvars so every log emitted deeper in the call
stack (by a handler, or later a worker job triggered from this update)
carries the same correlation fields without threading them through every
function signature.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.logging_conf import get_logger

logger = get_logger(__name__)


class LoggingMiddleware(BaseMiddleware):
    """Registered as an outer middleware on `dp.update`, so `event` here is
    always the raw `Update` — `data["event_from_user"]`/`event_chat` are
    populated by aiogram's own UserContextMiddleware, which (per aiogram's
    default middleware stack) always runs before any user-registered outer
    middleware.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        update = event if isinstance(event, Update) else None
        user = data.get("event_from_user")

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            update_id=getattr(update, "update_id", None),
            user_id=user.id if user else None,
        )

        start = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            duration_ms = round((time.monotonic() - start) * 1000, 1)
            logger.info("update_handled", event_type=update.event_type if update else None, duration_ms=duration_ms)
