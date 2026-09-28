"""Tests for the main menu reply keyboard, its cross-language matching
filter, and the language-selection inline keyboard."""

from __future__ import annotations

import pytest
from aiogram.types import Chat, Message
from aiogram.types import User as TgUser

from app.bot.callback_data import LanguageCallback
from app.bot.keyboards.language import build_language_keyboard
from app.bot.keyboards.menu import MenuButtonFilter, build_main_menu, menu_button_texts
from app.i18n.translator import Translator


def _message_with_text(text: str) -> Message:
    chat = Chat(id=1, type="private")
    user = TgUser(id=1, is_bot=False, first_name="T")
    return Message(message_id=1, date=0, chat=chat, from_user=user, text=text)


def test_build_main_menu_has_five_rows_for_regular_user() -> None:
    translator = Translator("en")
    menu = build_main_menu(translator, is_admin=False)

    # 3 rows: [find music, download media], [convert], [help, language]
    assert len(menu.keyboard) == 3
    all_texts = [btn.text for row in menu.keyboard for btn in row]
    assert translator.t("menu_find_music") in all_texts
    assert translator.t("menu_download_media") in all_texts
    assert translator.t("menu_convert_audio") in all_texts
    assert translator.t("menu_help") in all_texts
    assert translator.t("menu_language") in all_texts
    assert translator.t("menu_admin") not in all_texts


def test_build_main_menu_adds_admin_row_for_admin_user() -> None:
    translator = Translator("en")
    menu = build_main_menu(translator, is_admin=True)

    assert len(menu.keyboard) == 4
    all_texts = [btn.text for row in menu.keyboard for btn in row]
    assert translator.t("menu_admin") in all_texts


def test_build_main_menu_localized_per_language() -> None:
    uz_menu = build_main_menu(Translator("uz"))
    ru_menu = build_main_menu(Translator("ru"))

    uz_texts = {btn.text for row in uz_menu.keyboard for btn in row}
    ru_texts = {btn.text for row in ru_menu.keyboard for btn in row}
    assert uz_texts != ru_texts  # genuinely different labels per language


def test_menu_button_texts_includes_all_three_languages() -> None:
    texts = menu_button_texts("menu_help")

    assert Translator("uz").t("menu_help") in texts
    assert Translator("ru").t("menu_help") in texts
    assert Translator("en").t("menu_help") in texts
    assert len(texts) == 3


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_menu_button_filter_matches_any_supported_language(language: str) -> None:
    filter_ = MenuButtonFilter("menu_help")
    label = Translator(language).t("menu_help")

    assert await filter_(_message_with_text(label)) is True


async def test_menu_button_filter_matches_even_after_language_switch() -> None:
    """Regression scenario: user's stored language is now 'ru', but their
    on-screen keyboard still shows the OLD 'en' labels until Telegram redraws
    it -- the filter must still match the old label."""
    filter_ = MenuButtonFilter("menu_download_media")
    stale_english_label = Translator("en").t("menu_download_media")

    assert await filter_(_message_with_text(stale_english_label)) is True


async def test_menu_button_filter_rejects_unrelated_text() -> None:
    filter_ = MenuButtonFilter("menu_help")

    assert await filter_(_message_with_text("random unrelated text")) is False


async def test_menu_button_filter_rejects_none_text() -> None:
    filter_ = MenuButtonFilter("menu_help")
    chat = Chat(id=1, type="private")
    user = TgUser(id=1, is_bot=False, first_name="T")
    message = Message(message_id=1, date=0, chat=chat, from_user=user, sticker=None)  # text is None

    assert await filter_(message) is False


async def test_menu_button_filter_does_not_cross_match_different_keys() -> None:
    help_filter = MenuButtonFilter("menu_help")
    help_label_in_uz = Translator("uz").t("menu_help")
    download_label_in_uz = Translator("uz").t("menu_download_media")

    assert await help_filter(_message_with_text(help_label_in_uz)) is True
    assert await help_filter(_message_with_text(download_label_in_uz)) is False


# --- Language keyboard -----------------------------------------------------


def test_build_language_keyboard_has_three_options() -> None:
    keyboard = build_language_keyboard()

    assert len(keyboard.inline_keyboard) == 3
    languages = [LanguageCallback.unpack(row[0].callback_data).language for row in keyboard.inline_keyboard]
    assert set(languages) == {"uz", "ru", "en"}


def test_build_language_keyboard_labels_are_in_native_scripts() -> None:
    keyboard = build_language_keyboard()
    all_labels = " ".join(row[0].text for row in keyboard.inline_keyboard)

    assert "O'zbekcha" in all_labels
    assert "Русский" in all_labels
    assert "English" in all_labels
