"""Language-selection inline keyboard."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callback_data import LanguageCallback
from app.constants import SUPPORTED_LANGUAGES

# Displayed in each language's own native name, in its own script, regardless
# of the current UI language — a Russian speaker looking for their language
# should be able to recognize "Русский" even if the menu is currently in
# Uzbek or English.
_LANGUAGE_LABELS: dict[str, str] = {
    "uz": "🇺🇿 O'zbekcha",
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
}


def build_language_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=_LANGUAGE_LABELS[lang.value],
                callback_data=LanguageCallback(language=lang.value).pack(),
            )
        ]
        for lang in SUPPORTED_LANGUAGES
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
