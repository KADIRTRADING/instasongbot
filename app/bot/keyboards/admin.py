"""Inline keyboards for the admin panel (ARCHITECTURE.md §4.4 / §6)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.constants import MediaType, Platform
from app.db.models import CaptionButton, PlatformSetting
from app.i18n.translator import Translator

# Admin callback_data is simple, colon-separated, and never carries anything
# resembling a Telegram file_id or long URL, so none of these need the
# packed-reference-key treatment app/bot/callback_data.py uses for user-facing
# callbacks — the pieces here are always short, fixed vocabulary strings.
_PREFIX = "adm"


def build_admin_menu(translator: Translator) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=translator.t("admin_menu_captions"), callback_data=f"{_PREFIX}:captions")],
        [InlineKeyboardButton(text=translator.t("admin_menu_buttons"), callback_data=f"{_PREFIX}:buttons")],
        [InlineKeyboardButton(text=translator.t("admin_menu_platforms"), callback_data=f"{_PREFIX}:platforms")],
        [InlineKeyboardButton(text=translator.t("admin_menu_stats"), callback_data=f"{_PREFIX}:stats")],
        [InlineKeyboardButton(text=translator.t("admin_menu_limits"), callback_data=f"{_PREFIX}:limits")],
        [InlineKeyboardButton(text=translator.t("admin_menu_settings"), callback_data=f"{_PREFIX}:settings")],
        [InlineKeyboardButton(text=translator.t("admin_menu_broadcast"), callback_data=f"{_PREFIX}:broadcast")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_settings_keyboard(
    translator: Translator, *, quality: str, show_buttons: bool
) -> InlineKeyboardMarkup:
    """The bot-settings screen: current auto video quality + result-buttons
    toggle, each row tappable to change it. Shows the live values so an admin
    always sees the current state (re-read from the DB by the caller)."""
    quality_label = translator.t(f"admin_quality_{quality}")
    buttons_label = translator.t("admin_state_enabled" if show_buttons else "admin_state_disabled")
    rows = [
        [
            InlineKeyboardButton(
                text=translator.t("admin_settings_quality", value=quality_label),
                callback_data=f"{_PREFIX}:setquality",
            )
        ],
        [
            InlineKeyboardButton(
                text=translator.t("admin_settings_buttons", value=buttons_label),
                callback_data=f"{_PREFIX}:togglebtns",
            )
        ],
    ]
    return _with_back_row(InlineKeyboardMarkup(inline_keyboard=rows), translator)


def build_quality_picker(translator: Translator) -> InlineKeyboardMarkup:
    """One row per auto-download quality option."""
    from app.services.downloader.quality import VALID_QUALITIES

    rows = [
        [
            InlineKeyboardButton(
                text=translator.t(f"admin_quality_{quality}"),
                callback_data=f"{_PREFIX}:quality:{quality}",
            )
        ]
        for quality in VALID_QUALITIES
    ]
    return _with_back_row(InlineKeyboardMarkup(inline_keyboard=rows), translator)


def build_back_to_admin_menu_keyboard(translator: Translator) -> InlineKeyboardMarkup:
    """Appended under "browse and tap" admin screens (platforms/buttons/limits
    pickers) that aren't part of an FSM flow, so there's always a way back to
    the root admin menu without retyping /admin. FSM-driven screens (waiting
    for a text reply) use /cancel instead — see AdminCaptionStates etc. and
    the admin_invalid_url / admin_invalid_number i18n strings, which already
    point users at /cancel rather than a button tap.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=translator.t("back_to_menu"), callback_data=f"{_PREFIX}:menu")]]
    )


def _with_back_row(keyboard: InlineKeyboardMarkup, translator: Translator) -> InlineKeyboardMarkup:
    back_row = build_back_to_admin_menu_keyboard(translator).inline_keyboard[0]
    return InlineKeyboardMarkup(inline_keyboard=[*keyboard.inline_keyboard, back_row])


def build_media_type_picker(action: str, translator: Translator) -> InlineKeyboardMarkup:
    """Used by both "edit captions" and "add button" flows to pick which
    media type (video/audio/image) the change applies to. `action` namespaces
    the callback so the same three buttons can drive different flows.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=translator.t(f"media_type_{media_type.value}"),
                callback_data=f"{_PREFIX}:{action}:{media_type.value}",
            )
        ]
        for media_type in MediaType
    ]
    if action == "btnmedia":
        rows.append(
            [InlineKeyboardButton(text=translator.t("admin_all_media_types"), callback_data=f"{_PREFIX}:{action}:all")]
        )
    return _with_back_row(InlineKeyboardMarkup(inline_keyboard=rows), translator)


def build_platform_toggle_keyboard(
    platform_settings: dict[str, PlatformSetting | None], translator: Translator
) -> InlineKeyboardMarkup:
    """One row per platform, showing its current enabled/disabled state and
    toggling it on tap. `platform_settings` maps platform value -> the DB row
    (or None if never customized, which PlatformRepository treats as enabled).
    """
    rows = []
    for platform in Platform:
        row = platform_settings.get(platform.value)
        enabled = True if row is None else row.enabled
        icon = "✅" if enabled else "🚫"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {translator.t(f'platform_{platform.value}')}",
                    callback_data=f"{_PREFIX}:toggleplat:{platform.value}",
                )
            ]
        )
    return _with_back_row(InlineKeyboardMarkup(inline_keyboard=rows), translator)


def build_limits_picker(translator: Translator) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=translator.t(f"platform_{platform.value}"), callback_data=f"{_PREFIX}:setlimit:{platform.value}"
            )
        ]
        for platform in Platform
    ]
    rows.append(
        [InlineKeyboardButton(text=translator.t("admin_global_limit_label"), callback_data=f"{_PREFIX}:setlimit:global")]
    )
    return _with_back_row(InlineKeyboardMarkup(inline_keyboard=rows), translator)


def build_button_management_keyboard(buttons: list[CaptionButton], translator: Translator) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"🗑 {button.label} "
                    f"({translator.t(f'media_type_{button.media_type}') if button.media_type else translator.t('admin_all_media_types')})"
                ),
                callback_data=f"{_PREFIX}:rmbtn:{button.id}",
            )
        ]
        for button in buttons
    ]
    rows.append([InlineKeyboardButton(text=translator.t("admin_add_button"), callback_data=f"{_PREFIX}:addbtn")])
    return _with_back_row(InlineKeyboardMarkup(inline_keyboard=rows), translator)


def build_broadcast_confirm_keyboard(translator: Translator) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=translator.t("admin_confirm_send"), callback_data=f"{_PREFIX}:bcastconfirm"),
            InlineKeyboardButton(text=translator.t("admin_confirm_cancel"), callback_data=f"{_PREFIX}:bcastcancel"),
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
