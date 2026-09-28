"""Main menu (persistent reply keyboard) and its matching filters.

Reply-keyboard buttons send their label back as plain message text — there's
no callback_data to pack a stable identifier into. To avoid handlers being
silently unmatched right after a user switches language (the keyboard they
still see on screen was rendered in the OLD language until Telegram redraws
it), `menu_button_texts()` returns the button's text in every supported
language, and handlers filter on membership in that set rather than a single
exact string.
"""

from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.constants import SUPPORTED_LANGUAGES
from app.i18n.translator import Translator, get_translator

_MENU_KEYS = (
    "menu_find_music",
    "menu_download_media",
    "menu_convert_audio",
    "menu_help",
    "menu_language",
)


def build_main_menu(translator: Translator, *, is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=translator.t("menu_find_music")), KeyboardButton(text=translator.t("menu_download_media"))],
        [KeyboardButton(text=translator.t("menu_convert_audio"))],
        [KeyboardButton(text=translator.t("menu_help")), KeyboardButton(text=translator.t("menu_language"))],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=translator.t("menu_admin"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def menu_button_texts(key: str) -> frozenset[str]:
    """All translations of one menu_* key, across every supported language."""
    return frozenset(get_translator(lang.value).t(key) for lang in SUPPORTED_LANGUAGES)


class MenuButtonFilter(BaseFilter):
    """Matches a Message whose text equals the given menu key's label, in ANY
    supported language — not just the user's currently-stored one.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self._texts = menu_button_texts(key)

    async def __call__(self, message: Message) -> bool:
        return message.text is not None and message.text in self._texts
