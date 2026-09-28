"""Inline keyboards for the music-search and result-action flows.

The numbered-results keyboard is the heart of the text-search UX: up to 10
number buttons (bound to ABSOLUTE result indices so they survive paging),
plus Previous/Next when there's more than one page, plus Cancel. All buttons
carry a short session token (see app/bot/search_cache.py) so a tap resolves
back to the cached result list and can be checked against the requesting user.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callback_data import (
    ResultActionCallback,
    SearchCancelCallback,
    SearchPageCallback,
    SearchSelectCallback,
)
from app.bot.search_cache import SearchSession
from app.i18n.translator import Translator


def build_search_results_keyboard(token: str, session: SearchSession, page_index: int, translator: Translator) -> InlineKeyboardMarkup:
    """Number buttons for the given page + nav row + cancel row.

    Numbers are laid out in rows of 5 (so a full 10-result page is two tidy
    rows). Each number's callback carries the ABSOLUTE index into the full
    result list, computed from the page offset, so "3" on page 2 selects the
    right track.
    """
    page_index = max(0, min(page_index, session.total_pages - 1))
    page_results = session.page(page_index)
    offset = page_index * session.per_page

    number_buttons = [
        InlineKeyboardButton(
            text=str(i + 1),
            callback_data=SearchSelectCallback(token=token, index=offset + i).pack(),
        )
        for i in range(len(page_results))
    ]
    rows: list[list[InlineKeyboardButton]] = [number_buttons[:5], number_buttons[5:]]
    rows = [row for row in rows if row]  # drop an empty second row for <=5 results

    nav_row: list[InlineKeyboardButton] = []
    if page_index > 0:
        nav_row.append(
            InlineKeyboardButton(
                text=translator.t("search_btn_prev"),
                callback_data=SearchPageCallback(token=token, page=page_index - 1).pack(),
            )
        )
    if page_index < session.total_pages - 1:
        nav_row.append(
            InlineKeyboardButton(
                text=translator.t("search_btn_next"),
                callback_data=SearchPageCallback(token=token, page=page_index + 1).pack(),
            )
        )
    if nav_row:
        rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(
                text=translator.t("search_btn_cancel"),
                callback_data=SearchCancelCallback(token=token).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_official_link_keyboard(url: str, translator: Translator) -> InlineKeyboardMarkup:
    """A single "open full song" URL button, shown under a preview clip or in
    place of one when no preview is available."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=translator.t("search_open_official"), url=url)]]
    )


def build_result_actions_keyboard(token: str, translator: Translator, *, has_file: bool) -> InlineKeyboardMarkup:
    """Optional follow-up actions under an auto-downloaded social video.

    "Find this song" and "Extract MP3" both reuse the delivered file, so they
    only appear when we actually have a file_id to reuse (`has_file`). "Other
    quality/options" re-probes and is always available.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if has_file:
        rows.append(
            [
                InlineKeyboardButton(
                    text=translator.t("result_action_find_song"),
                    callback_data=ResultActionCallback(token=token, action="find").pack(),
                ),
                InlineKeyboardButton(
                    text=translator.t("result_action_extract_mp3"),
                    callback_data=ResultActionCallback(token=token, action="mp3").pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=translator.t("result_action_other_options"),
                callback_data=ResultActionCallback(token=token, action="other").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
