"""Opens one short-lived DB session per update and injects it as `session` in
handler kwargs. Handlers/other middlewares never construct a session
themselves — this is the one place that does, so session lifetime is always
exactly "one update" (commit on success, rollback on exception), matching the
short-lived-session pattern used throughout app/db/session.py.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from app.db.session import get_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
