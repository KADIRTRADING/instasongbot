"""Social-media auto-download handler.

Automatic UX (see the project brief / ARCHITECTURE.md §4.2): any message whose
text contains a supported-platform URL is downloaded IMMEDIATELY — probe, pick
the admin-configured quality, fetch, and deliver the video — with NO
format-selection step. The single `auto_download_job` does all of that in the
worker so the user just gets their video back.

Optional follow-up actions ("Find this song", "Extract MP3", "Other
quality/options") are offered as inline buttons UNDER the delivered video (see
app/bot/keyboards/search.py + app/bot/result_cache.py), gated by the admin
`show_result_buttons` setting. "Other quality/options" is the escape hatch
back to the old explicit format keyboard, which still exists
(DownloadFormatCallback) for anyone who wants a specific format.

Content-type routing, not FSM — same rationale as handlers/recognize.py. The
supported-link filter excludes nothing else: a plain-text message with no URL
falls through to handlers/search.py (music search).
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import CallbackQuery, Message
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callback_data import (
    CallbackDataError,
    DownloadFormatCallback,
    ResultActionCallback,
    matches_prefix,
)
from app.bot.probe_cache import load_probe_result
from app.bot.result_cache import load_result_context
from app.config import Settings
from app.constants import JobType
from app.db.repositories import JobRepository, PlatformRepository
from app.i18n.translator import Translator
from app.services.downloader.url_utils import detect_platform, extract_first_url, normalize_url

router = Router(name="download")


def _has_supported_link(message: Message) -> bool:
    if not message.text:
        return False
    url = extract_first_url(message.text)
    return url is not None and detect_platform(normalize_url(url)) is not None


@router.message(F.text, _has_supported_link)
@flags.rate_limit("download")
async def handle_link(
    message: Message,
    session: AsyncSession,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    raw_url = extract_first_url(message.text or "")
    assert raw_url is not None  # guaranteed by the _has_supported_link filter
    url = normalize_url(raw_url)

    platform = detect_platform(url)
    assert platform is not None  # guaranteed by the _has_supported_link filter

    if not await PlatformRepository.is_enabled(session, platform.value):
        await message.answer(translator.t("error_platform_disabled"))
        return

    friendly = translator.t(f"platform_{platform.value}")
    progress_message = await message.answer(translator.t("auto_download_started", platform=friendly))

    job_id = str(uuid4())
    await JobRepository.create(
        session,
        job_id=job_id,
        user_id=message.from_user.id,
        job_type=JobType.DOWNLOAD.value,
        platform=platform.value,
        source_url=url,
    )

    # One job does probe -> auto-pick quality -> download -> deliver, plus
    # attaching the optional result-action buttons. No user format tap.
    await arq_pool.enqueue_job(
        "auto_download_job",
        job_id=job_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=progress_message.message_id,
        url=url,
    )


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, ResultActionCallback.PREFIX))
@flags.rate_limit("download")
async def on_result_action(
    callback: CallbackQuery,
    session: AsyncSession,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    """Handle the "Find this song" / "Extract MP3" / "Other quality/options"
    buttons attached under an auto-downloaded video. Reuses the already-
    delivered media (via its cached Telegram file_id) rather than refetching."""
    try:
        data = ResultActionCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    context = await load_result_context(arq_pool, data.token)
    if context is None:
        await callback.answer(translator.t("result_actions_expired"), show_alert=True)
        return
    if not context.belongs_to(callback.from_user.id):
        await callback.answer()
        return

    if data.action == "other":
        # Re-show the explicit format keyboard from the cached probe result.
        probe = await load_probe_result(arq_pool, context.probe_job_id) if context.probe_job_id else None
        if probe is None:
            await callback.answer(translator.t("result_actions_expired"), show_alert=True)
            return
        from app.bot.keyboards.download import build_format_keyboard

        await callback.answer()
        keyboard = build_format_keyboard(context.probe_job_id, probe, translator)
        await callback.message.answer(translator.t("choose_download_option"), reply_markup=keyboard)
        return

    # "find" or "mp3" — both reuse the delivered file_id, so we need one.
    if not context.file_id:
        await callback.answer(translator.t("result_actions_expired"), show_alert=True)
        return

    await callback.answer()
    if callback.message is None:
        return

    if data.action == "find":
        progress = await callback.message.answer(translator.t("recognizing_in_progress"))
        job_type = JobType.RECOGNIZE.value
        job_name = "recognize_job"
    else:  # "mp3"
        progress = await callback.message.answer(translator.t("converting_in_progress"))
        job_type = JobType.CONVERT.value
        job_name = "convert_job"

    job_id = str(uuid4())
    await JobRepository.create(session, job_id=job_id, user_id=callback.from_user.id, job_type=job_type)
    await arq_pool.enqueue_job(
        job_name,
        job_id=job_id,
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        message_id=progress.message_id,
        source_file_id=context.file_id,
    )


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, DownloadFormatCallback.PREFIX))
@flags.rate_limit("download")
async def on_format_selected(
    callback: CallbackQuery,
    session: AsyncSession,
    translator: Translator,
    settings: Settings,
    arq_pool: ArqRedis,
) -> None:
    """Explicit format pick — reached via the "Other quality/options" escape
    hatch (and for multi-image carousels, where auto-picking one image would
    be wrong: the user chooses which image, or "download all")."""
    try:
        data = DownloadFormatCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    probe = await load_probe_result(arq_pool, data.probe_job_id)
    if probe is None:
        await callback.answer(translator.t("error_content_not_found"), show_alert=True)
        return

    await callback.answer()
    if callback.message is None:
        return

    if data.format_index == "all":
        format_ids = [fmt.format_id for fmt in probe.formats]
    else:
        if not (0 <= data.format_index < len(probe.formats)):
            await callback.answer(translator.t("error_content_not_found"), show_alert=True)
            return
        format_ids = [probe.formats[data.format_index].format_id]

    # Each queued download gets its OWN progress message. Reusing one shared
    # message_id across multiple concurrent download_job runs (the "download
    # all" case) would race: the first job to finish deletes/edits that
    # message, and every other job's edit/delete on the now-gone message_id
    # fails — the files would still reach the user, but their jobs would be
    # misleadingly recorded as "failed" even though delivery succeeded.
    await callback.message.edit_text(translator.t("downloading_in_progress"))

    for index, format_id in enumerate(format_ids):
        progress_message = (
            callback.message
            if index == 0
            else await callback.message.answer(translator.t("downloading_in_progress"))
        )

        job_id = str(uuid4())
        await JobRepository.create(
            session,
            job_id=job_id,
            user_id=callback.from_user.id,
            job_type=JobType.DOWNLOAD.value,
            platform=probe.platform,
            source_url=probe.source_url,
        )
        await arq_pool.enqueue_job(
            "download_job",
            job_id=job_id,
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,
            message_id=progress_message.message_id,
            probe_job_id=data.probe_job_id,
            format_id=format_id,
        )
