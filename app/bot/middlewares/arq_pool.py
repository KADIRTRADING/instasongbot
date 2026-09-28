"""Injects the shared arq Redis connection pool (used to enqueue background
jobs) as `arq_pool` in every handler's kwargs — the bot-side counterpart to
the worker's own on_startup-built resources (see app/workers/settings.py).
One pool is created once at bot startup (see the dispatcher factory) and
handed to every update via this middleware, rather than each handler opening
its own connection.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from arq.connections import ArqRedis


class ArqPoolMiddleware(BaseMiddleware):
    def __init__(self, arq_pool: ArqRedis) -> None:
        self._pool = arq_pool

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["arq_pool"] = self._pool
        return await handler(event, data)
