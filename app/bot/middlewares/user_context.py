"""Loads/creates the DB user row for whoever triggered this update and
attaches a bound `Translator` (in the user's stored language) to the handler
kwargs. Runs after DbSessionMiddleware (needs `data["session"]`) and relies on
aiogram's own UserContextMiddleware having already populated
`data["event_from_user"]` (true for every Update type — verified against
aiogram's default middleware stack).

Also enforces the ban check here: rather than a separate middleware, banning
is folded into this one since both need the same DB lookup — no reason to
hit the users table twice per update.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from app.config import Settings
from app.db.repositories import UserRepository
from app.i18n.translator import Translator, get_translator


class UserContextMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            # Update types with no associated user (e.g. a poll result from an
            # anonymous voter) — nothing to load, just continue.
            return await handler(event, data)

        session = data["session"]
        user = await UserRepository.get_or_create(
            session,
            user_id=tg_user.id,
            username=tg_user.username,
            first_name=tg_user.first_name,
            default_language=self._settings.DEFAULT_LANGUAGE,
        )

        if user.is_banned:
            translator = get_translator(user.language_code)
            await _notify_banned(event, translator)
            return None

        data["db_user"] = user
        data["translator"] = translator = get_translator(user.language_code)
        data["is_admin"] = tg_user.id in self._settings.admin_ids
        return await handler(event, data)


async def _notify_banned(event: TelegramObject, translator: Translator) -> None:
    """`event` here is always the raw `Update` — this middleware is
    registered as an outer middleware on `dp.update` (see the dispatcher
    factory), so it must unwrap the actual Message/CallbackQuery itself
    rather than isinstance-checking `event` directly (a bug caught by a live
    test: isinstance(event, Message) is always False at this level, since
    `event` is an Update, not a Message).
    """
    text = translator.t("error_banned")
    if not isinstance(event, Update):
        return
    if event.message is not None:
        await event.message.answer(text)
    elif event.callback_query is not None:
        await event.callback_query.answer(text, show_alert=True)
