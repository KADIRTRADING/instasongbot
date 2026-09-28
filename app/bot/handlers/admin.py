"""Admin panel handlers (ARCHITECTURE.md §4.5 / §6).

Unlike the user-facing flows, this router genuinely needs FSM (see
app/bot/states.py's docstring for the rationale) and is gated end-to-end: the
whole router is filtered on `is_admin` (injected by UserContextMiddleware —
see app/bot/middlewares/user_context.py) via `router.message.filter(...)` /
`router.callback_query.filter(...)`, so a non-admin's `/admin` or a stray
`adm:...` callback (which a non-admin could never actually receive a
keyboard for, but is checked anyway — defense in depth) simply falls through
un-handled rather than reaching any admin logic. This mirrors the verified
aiogram behavior that a router-level `.filter()` can request injected
dependencies (`is_admin: bool`) exactly like a handler can (confirmed live
against a real Dispatcher during development).

Every sub-flow follows the same shape:
  1. A menu-tap callback (`adm:xxx`) shows a picker or prompt.
  2. For anything requiring free-text input, we set an FSM state
     (app/bot/states.py) and the next plain-text message is routed by that
     state, not by content-type (this IS the case FSM earns its keep for).
  3. `/cancel` is honored from every waiting-for-input state and returns to
     the admin menu.

All writes go through the repository layer (app/db/repositories.py) — no raw
queries here — and every screen re-reads from the DB rather than trusting
stale in-memory state, since another admin could be editing concurrently.
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin import (
    build_admin_menu,
    build_back_to_admin_menu_keyboard,
    build_broadcast_confirm_keyboard,
    build_button_management_keyboard,
    build_limits_picker,
    build_media_type_picker,
    build_platform_toggle_keyboard,
    build_quality_picker,
    build_settings_keyboard,
)
from app.bot.settings_store import (
    get_auto_video_quality,
    get_show_result_buttons,
    set_auto_video_quality,
    set_show_result_buttons,
)
from app.bot.states import (
    AdminBroadcastStates,
    AdminButtonStates,
    AdminCaptionStates,
    AdminLimitStates,
)
from app.config import Settings
from app.constants import MediaType, Platform
from app.db.repositories import (
    BotSettingRepository,
    BroadcastRepository,
    CaptionRepository,
    PlatformRepository,
    StatsRepository,
    UserRepository,
)
from app.i18n.translator import Translator
from app.services.captions.renderer import CaptionContext, CaptionRenderer
from app.services.downloader.quality import VALID_QUALITIES

router = Router(name="admin")


async def _require_admin(*_event: object, is_admin: bool = False) -> bool:
    """Router-level gate shared by both `.message.filter()` and
    `.callback_query.filter()` (see module docstring). The leading `*_event`
    is required, not cosmetic: aiogram's DI (see
    `CallableObject._prepare_kwargs`/`.call` in aiogram's own
    dispatcher/event/handler.py) always passes the triggering event as the
    first *positional* argument. Without an explicit positional parameter to
    absorb it, that positional argument collides with `is_admin` (itself
    supplied as a keyword) and raises "got multiple values for argument" —
    caught by a live Dispatcher test during development.
    """
    return is_admin


router.message.filter(_require_admin)
router.callback_query.filter(_require_admin)


def _valid_media_type(value: str) -> bool:
    return value in {m.value for m in MediaType}


def _valid_platform(value: str) -> bool:
    return value in {p.value for p in Platform}


# --- Entry point -----------------------------------------------------------


@router.message(Command("admin"))
async def cmd_admin(message: Message, translator: Translator, state: FSMContext) -> None:
    await state.clear()  # a fresh /admin always abandons any half-finished flow
    await message.answer(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))


@router.callback_query(F.data == "adm:menu")
async def on_back_to_menu(callback: CallbackQuery, translator: Translator, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))


@router.message(Command("cancel"), StateFilter(AdminCaptionStates, AdminButtonStates, AdminLimitStates, AdminBroadcastStates))
async def cmd_cancel(message: Message, translator: Translator, state: FSMContext) -> None:
    await state.clear()
    await message.answer(translator.t("admin_operation_cancelled"), reply_markup=build_admin_menu(translator))


# --- Captions ---------------------------------------------------------------


@router.callback_query(F.data == "adm:captions")
async def on_captions_menu(callback: CallbackQuery, translator: Translator) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_pick_media_type"), reply_markup=build_media_type_picker("capmedia", translator)
        )


@router.callback_query(F.data.startswith("adm:capmedia:"))
async def on_caption_media_type_selected(
    callback: CallbackQuery, session: AsyncSession, translator: Translator, state: FSMContext
) -> None:
    media_type = callback.data.rsplit(":", maxsplit=1)[-1]
    if not _valid_media_type(media_type):
        await callback.answer()
        return

    current = await CaptionRepository.get_template(session, media_type)
    await state.set_state(AdminCaptionStates.waiting_for_template_text)
    await state.update_data(media_type=media_type)

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_send_new_template", media_type=translator.t(f"media_type_{media_type}"), current=current)
        )


@router.message(StateFilter(AdminCaptionStates.waiting_for_template_text), F.text)
async def on_caption_template_text(
    message: Message, session: AsyncSession, translator: Translator, state: FSMContext
) -> None:
    data = await state.get_data()
    media_type = data.get("media_type")
    if not media_type:  # pragma: no cover - defensive: state without its data would be a bug elsewhere
        await state.clear()
        return

    new_template = message.text or ""
    await CaptionRepository.set_template(session, media_type, new_template, updated_by=message.from_user.id)
    await state.clear()

    preview = CaptionRenderer.render_text(
        new_template,
        CaptionContext(title="Sample Title", artist="Sample Artist", source="Sample Source", bot_username="samplebot"),
    )
    await message.answer(translator.t("admin_saved"))
    await message.answer(translator.t("admin_template_preview", preview=preview))
    await message.answer(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))


# --- Buttons -----------------------------------------------------------------


@router.callback_query(F.data == "adm:buttons")
async def on_buttons_menu(callback: CallbackQuery, session: AsyncSession, translator: Translator) -> None:
    buttons = await CaptionRepository.list_all_buttons(session)
    await callback.answer()
    if callback.message is None:
        return
    if buttons:
        await callback.message.edit_text(
            translator.t("admin_menu_buttons"), reply_markup=build_button_management_keyboard(buttons, translator)
        )
    else:
        await callback.message.edit_text(
            translator.t("admin_no_buttons_yet"),
            reply_markup=build_button_management_keyboard([], translator),
        )


@router.callback_query(F.data.startswith("adm:rmbtn:"))
async def on_remove_button(callback: CallbackQuery, session: AsyncSession, translator: Translator) -> None:
    button_id_raw = callback.data.rsplit(":", maxsplit=1)[-1]
    try:
        button_id = int(button_id_raw)
    except ValueError:
        await callback.answer()
        return

    await CaptionRepository.remove_button(session, button_id)
    await callback.answer(translator.t("admin_button_removed"))

    buttons = await CaptionRepository.list_all_buttons(session)
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_menu_buttons") if buttons else translator.t("admin_no_buttons_yet"),
            reply_markup=build_button_management_keyboard(buttons, translator),
        )


@router.callback_query(F.data == "adm:addbtn")
async def on_add_button_start(callback: CallbackQuery, translator: Translator, state: FSMContext) -> None:
    await state.set_state(AdminButtonStates.waiting_for_media_type)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_pick_button_media_type"), reply_markup=build_media_type_picker("btnmedia", translator)
        )


@router.callback_query(StateFilter(AdminButtonStates.waiting_for_media_type), F.data.startswith("adm:btnmedia:"))
async def on_add_button_media_type_selected(
    callback: CallbackQuery, translator: Translator, state: FSMContext
) -> None:
    media_type_raw = callback.data.rsplit(":", maxsplit=1)[-1]
    media_type = None if media_type_raw == "all" else media_type_raw
    if media_type is not None and not _valid_media_type(media_type):
        await callback.answer()
        return

    await state.update_data(media_type=media_type)
    await state.set_state(AdminButtonStates.waiting_for_label)

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_send_button_label"))


@router.message(StateFilter(AdminButtonStates.waiting_for_label), F.text)
async def on_add_button_label(message: Message, translator: Translator, state: FSMContext) -> None:
    await state.update_data(label=message.text)
    await state.set_state(AdminButtonStates.waiting_for_url)
    await message.answer(translator.t("admin_send_button_url"))


@router.message(StateFilter(AdminButtonStates.waiting_for_url), F.text)
async def on_add_button_url(message: Message, session: AsyncSession, translator: Translator, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(translator.t("admin_invalid_url"))
        return

    data = await state.get_data()
    label = data.get("label", "")
    media_type = data.get("media_type")

    existing = await CaptionRepository.list_all_buttons(session)
    next_position = len(existing)

    await CaptionRepository.add_button(session, label=label, url=url, media_type=media_type, position=next_position)
    await state.clear()

    await message.answer(translator.t("admin_button_added", label=label))
    await message.answer(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))


# --- Platforms ---------------------------------------------------------------


@router.callback_query(F.data == "adm:platforms")
async def on_platforms_menu(callback: CallbackQuery, session: AsyncSession, translator: Translator) -> None:
    rows = await PlatformRepository.list_all(session)
    settings_by_platform = {row.platform: row for row in rows}
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_menu_platforms"),
            reply_markup=build_platform_toggle_keyboard(settings_by_platform, translator),
        )


@router.callback_query(F.data.startswith("adm:toggleplat:"))
async def on_toggle_platform(callback: CallbackQuery, session: AsyncSession, translator: Translator) -> None:
    platform = callback.data.rsplit(":", maxsplit=1)[-1]
    if not _valid_platform(platform):
        await callback.answer()
        return

    currently_enabled = await PlatformRepository.is_enabled(session, platform)
    await PlatformRepository.set_enabled(session, platform, not currently_enabled)

    new_state_key = "admin_state_disabled" if currently_enabled else "admin_state_enabled"
    await callback.answer(
        translator.t("admin_platform_toggled", platform=translator.t(f"platform_{platform}"), state=translator.t(new_state_key))
    )

    rows = await PlatformRepository.list_all(session)
    settings_by_platform = {row.platform: row for row in rows}
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_menu_platforms"),
            reply_markup=build_platform_toggle_keyboard(settings_by_platform, translator),
        )


# --- Stats -------------------------------------------------------------------


@router.callback_query(F.data == "adm:stats")
async def on_stats(callback: CallbackQuery, session: AsyncSession, translator: Translator) -> None:
    stats = await StatsRepository.compute(session)

    by_type = "\n".join(f"  {job_type}: {count}" for job_type, count in sorted(stats.jobs_by_type.items())) or "  —"
    by_platform = (
        "\n".join(f"  {translator.t(f'platform_{p}')}: {c}" for p, c in sorted(stats.jobs_by_platform.items())) or "  —"
    )

    text = translator.t(
        "admin_stats_text",
        total_users=stats.total_users,
        new_users_7d=stats.new_users_7d,
        jobs_total=stats.jobs_total,
        jobs_completed=stats.jobs_completed,
        jobs_failed=stats.jobs_failed,
        by_type=by_type,
        by_platform=by_platform,
    )

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(text, reply_markup=build_back_to_admin_menu_keyboard(translator))


# --- File size limits ---------------------------------------------------------


@router.callback_query(F.data == "adm:limits")
async def on_limits_menu(callback: CallbackQuery, translator: Translator) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_pick_limit_target"), reply_markup=build_limits_picker(translator))


@router.callback_query(F.data.startswith("adm:setlimit:"))
async def on_limit_target_selected(callback: CallbackQuery, translator: Translator, state: FSMContext) -> None:
    target = callback.data.rsplit(":", maxsplit=1)[-1]
    if target != "global" and not _valid_platform(target):
        await callback.answer()
        return

    await state.set_state(AdminLimitStates.waiting_for_limit_value)
    await state.update_data(target=target)

    display_target = translator.t("admin_global_limit_label") if target == "global" else translator.t(f"platform_{target}")
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_send_new_limit_mb", target=display_target))


@router.message(StateFilter(AdminLimitStates.waiting_for_limit_value), F.text)
async def on_limit_value(message: Message, session: AsyncSession, translator: Translator, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.lstrip("-").isdigit() or int(raw) <= 0:
        await message.answer(translator.t("admin_invalid_number"))
        return

    limit_mb = int(raw)
    data = await state.get_data()
    target = data.get("target", "global")

    if target == "global":
        await BotSettingRepository.set(session, "max_download_mb", limit_mb, updated_by=message.from_user.id)
        display_target = translator.t("admin_global_limit_label")
    else:
        await PlatformRepository.set_max_file_size_mb(session, target, limit_mb)
        display_target = translator.t(f"platform_{target}")

    await state.clear()
    await message.answer(translator.t("admin_limit_updated", target=display_target, limit_mb=limit_mb))
    await message.answer(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))


# --- Bot settings (auto quality / result buttons) ----------------------------


async def _show_settings(callback: CallbackQuery, session: AsyncSession, translator: Translator, settings: Settings) -> None:
    quality = await get_auto_video_quality(session, env_default=settings.AUTO_VIDEO_QUALITY)
    show_buttons = await get_show_result_buttons(session)
    if callback.message is not None:
        await callback.message.edit_text(
            translator.t("admin_settings_title"),
            reply_markup=build_settings_keyboard(translator, quality=quality, show_buttons=show_buttons),
        )


@router.callback_query(F.data == "adm:settings")
async def on_settings_menu(callback: CallbackQuery, session: AsyncSession, translator: Translator, settings: Settings) -> None:
    await callback.answer()
    await _show_settings(callback, session, translator, settings)


@router.callback_query(F.data == "adm:setquality")
async def on_pick_quality(callback: CallbackQuery, translator: Translator) -> None:
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_pick_quality"), reply_markup=build_quality_picker(translator))


@router.callback_query(F.data.startswith("adm:quality:"))
async def on_quality_selected(callback: CallbackQuery, session: AsyncSession, translator: Translator, settings: Settings) -> None:
    quality = callback.data.rsplit(":", maxsplit=1)[-1]
    if quality not in VALID_QUALITIES:
        await callback.answer()
        return
    await set_auto_video_quality(session, quality, updated_by=callback.from_user.id)
    await callback.answer(translator.t("admin_quality_updated", value=translator.t(f"admin_quality_{quality}")))
    await _show_settings(callback, session, translator, settings)


@router.callback_query(F.data == "adm:togglebtns")
async def on_toggle_result_buttons(callback: CallbackQuery, session: AsyncSession, translator: Translator, settings: Settings) -> None:
    current = await get_show_result_buttons(session)
    await set_show_result_buttons(session, not current, updated_by=callback.from_user.id)
    state_key = "admin_state_disabled" if current else "admin_state_enabled"
    await callback.answer(translator.t("admin_buttons_toggled", state=translator.t(state_key)))
    await _show_settings(callback, session, translator, settings)


# --- Broadcast ---------------------------------------------------------------


@router.callback_query(F.data == "adm:broadcast")
async def on_broadcast_start(callback: CallbackQuery, translator: Translator, state: FSMContext) -> None:
    await state.set_state(AdminBroadcastStates.waiting_for_text)
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_send_broadcast_text"))


@router.message(StateFilter(AdminBroadcastStates.waiting_for_text), F.text)
async def on_broadcast_text(message: Message, session: AsyncSession, translator: Translator, state: FSMContext) -> None:
    text = message.text or ""
    user_count = len(await UserRepository.iter_all_ids(session, exclude_banned=True))

    await state.update_data(text=text)
    await state.set_state(AdminBroadcastStates.waiting_for_confirmation)

    await message.answer(
        translator.t("admin_broadcast_preview", text=text, user_count=user_count),
        reply_markup=build_broadcast_confirm_keyboard(translator),
    )


@router.callback_query(StateFilter(AdminBroadcastStates.waiting_for_confirmation), F.data == "adm:bcastconfirm")
async def on_broadcast_confirm(
    callback: CallbackQuery, session: AsyncSession, translator: Translator, state: FSMContext, arq_pool: ArqRedis
) -> None:
    data = await state.get_data()
    text = data.get("text", "")
    await state.clear()

    user_count = len(await UserRepository.iter_all_ids(session, exclude_banned=True))

    broadcast_id = str(uuid4())
    await BroadcastRepository.create(session, broadcast_id=broadcast_id, admin_id=callback.from_user.id, message_text=text)
    await arq_pool.enqueue_job("broadcast_job", broadcast_id=broadcast_id)

    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_broadcast_started", user_count=user_count))
        await callback.message.answer(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))


@router.callback_query(StateFilter(AdminBroadcastStates.waiting_for_confirmation), F.data == "adm:bcastcancel")
async def on_broadcast_cancel(callback: CallbackQuery, translator: Translator, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message is not None:
        await callback.message.edit_text(translator.t("admin_broadcast_cancelled_by_admin"))
        await callback.message.answer(translator.t("admin_menu_title"), reply_markup=build_admin_menu(translator))
