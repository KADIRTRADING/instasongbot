"""Core handlers: /start, /help, the Help and Language menu buttons, and the
language-selection callback. These don't enqueue any background job — they
only ever answer directly and instantly.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callback_data import CallbackDataError, LanguageCallback, matches_prefix
from app.bot.keyboards.language import build_language_keyboard
from app.bot.keyboards.menu import MenuButtonFilter, build_main_menu
from app.db.models import User
from app.db.repositories import UserRepository
from app.i18n.translator import Translator, get_translator

router = Router(name="core")


@router.message(CommandStart())
async def cmd_start(message: Message, db_user: User, translator: Translator, is_admin: bool) -> None:
    name = message.from_user.first_name if message.from_user else ""
    await message.answer(
        translator.t("welcome", name=name),
        reply_markup=build_main_menu(translator, is_admin=is_admin),
    )


@router.message(Command("help"))
@router.message(MenuButtonFilter("menu_help"))
async def cmd_help(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("help_text"))


@router.message(Command("language"))
@router.message(MenuButtonFilter("menu_language"))
async def cmd_language(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("language_prompt"), reply_markup=build_language_keyboard())


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, LanguageCallback.PREFIX))
async def on_language_selected(
    callback: CallbackQuery, session: AsyncSession, db_user: User, is_admin: bool
) -> None:
    try:
        data = LanguageCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    await UserRepository.set_language(session, db_user.id, data.language)
    new_translator = get_translator(data.language)

    await callback.answer(new_translator.t("language_changed"))
    if callback.message is not None:
        await callback.message.edit_text(new_translator.t("language_prompt"))
        # The persistent reply keyboard only redraws when a NEW message
        # carries a `reply_markup` — editing the old message can't do that,
        # so send one extra, brief message that carries the refreshed menu
        # in the new language.
        await callback.message.answer(
            new_translator.t("welcome", name=callback.from_user.first_name or ""),
            reply_markup=build_main_menu(new_translator, is_admin=is_admin),
        )
