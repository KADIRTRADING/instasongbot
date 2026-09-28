"""Social-media download handler: detects a supported link in ANY message
text (not just after tapping "Download Media" — see ARCHITECTURE.md §4.2),
enqueues a `probe_job`, and wires the resulting inline-keyboard callbacks
(pick one format, or "download all" for a multi-image carousel) to a
`download_job`.

Content-type routing, not FSM — same rationale as handlers/recognize.py.
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import CallbackQuery, Message
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callback_data import CallbackDataError, DownloadFormatCallback, matches_prefix
from app.bot.keyboards.menu import MenuButtonFilter
from app.bot.probe_cache import load_probe_result
from app.config import Settings
from app.constants import JobType
from app.db.repositories import JobRepository, PlatformRepository
from app.i18n.translator import Translator
from app.services.downloader.url_utils import detect_platform, extract_first_url

router = Router(name="download")


@router.message(MenuButtonFilter("menu_download_media"))
async def prompt_download_media(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("ask_send_link"))


def _has_supported_link(message: Message) -> bool:
    if not message.text:
        return False
    url = extract_first_url(message.text)
    return url is not None and detect_platform(url) is not None


@router.message(F.text, _has_supported_link)
@flags.rate_limit("download")
async def handle_link(
    message: Message,
    session: AsyncSession,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    url = extract_first_url(message.text or "")
    assert url is not None  # guaranteed by the _has_supported_link filter

    platform = detect_platform(url)
    assert platform is not None  # guaranteed by the _has_supported_link filter

    if not await PlatformRepository.is_enabled(session, platform.value):
        await message.answer(translator.t("error_platform_disabled"))
        return

    progress_message = await message.answer(translator.t("probing_link"))

    job_id = str(uuid4())
    await JobRepository.create(
        session, job_id=job_id, user_id=message.from_user.id, job_type=JobType.PROBE.value, platform=platform.value, source_url=url
    )

    await arq_pool.enqueue_job(
        "probe_job",
        job_id=job_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=progress_message.message_id,
        url=url,
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
