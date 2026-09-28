"""Core handlers: /start, /help, /language, and the language-selection
callback. These don't enqueue any background job — they only ever answer
directly and instantly.

There is deliberately NO persistent reply-keyboard menu anymore (the bot's
UX is fully automatic content routing — see ARCHITECTURE.md §4 and each
handler's docstring). /start and /help just explain what to send; the bot
figures out the rest from the message's content.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callback_data import CallbackDataError, LanguageCallback, matches_prefix
from app.bot.keyboards.language import build_language_keyboard
from app.db.models import User
from app.i18n.translator import Translator, get_translator

router = Router(name="core")


@router.message(CommandStart())
async def cmd_start(message: Message, translator: Translator) -> None:
    name = message.from_user.first_name if message.from_user else ""
    await message.answer(translator.t("welcome", name=name))


@router.message(Command("help"))
async def cmd_help(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("help_text"))


@router.message(Command("language"))
async def cmd_language(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("language_prompt"), reply_markup=build_language_keyboard())


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, LanguageCallback.PREFIX))
async def on_language_selected(callback: CallbackQuery, session: AsyncSession, db_user: User) -> None:
    try:
        data = LanguageCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    from app.db.repositories import UserRepository

    await UserRepository.set_language(session, db_user.id, data.language)
    new_translator = get_translator(data.language)

    await callback.answer(new_translator.t("language_changed"))
    if callback.message is not None:
        # No reply keyboard to redraw anymore — just confirm in the new
        # language and re-show the welcome/routing hint so the user sees the
        # switch took effect.
        await callback.message.edit_text(new_translator.t("language_changed"))
        await callback.message.answer(
            new_translator.t("welcome", name=callback.from_user.first_name or "")
        )
