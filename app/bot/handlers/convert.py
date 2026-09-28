"""Video-to-audio / video-tools handler (ARCHITECTURE.md §4.3).

Two entry points converge on the same `VideoActionCallback`-driven choice:
  - An uploaded video file (F.video) -> stash its file_id in upload_cache,
    offer "Identify song" / "Extract audio" (no "download original": the
    user already has that exact file).
  - A supported video LINK is already handled by handlers/download.py's
    `probe_job`; when that probe comes back containing a video format, the
    worker (see app/workers/tasks.py's probe_job) shows the 3-way keyboard
    instead of a plain format list, offering "Identify song" / "Extract
    audio" / "Download original video" — all three, since a link download
    is optional here.

This file owns: the upload entry point, the "Convert Video to Audio" menu
prompt, and the single VideoActionCallback handler that dispatches to
recognize_job / convert_job / download_job depending on the chosen action.
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import CallbackQuery, Message
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callback_data import CallbackDataError, VideoActionCallback, matches_prefix
from app.bot.keyboards.download import build_video_action_keyboard
from app.bot.keyboards.menu import MenuButtonFilter
from app.bot.probe_cache import load_probe_result
from app.bot.upload_cache import load_upload_file_id, store_upload_file_id
from app.constants import JobType
from app.db.repositories import JobRepository
from app.i18n.translator import Translator

router = Router(name="convert")


@router.message(MenuButtonFilter("menu_convert_audio"))
async def prompt_convert_audio(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("ask_send_video_for_conversion"))


@router.message(F.video)
@flags.rate_limit("convert")
async def handle_video_upload(
    message: Message,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    video = message.video
    if video is None:  # pragma: no cover - filter guarantees this is set
        return

    upload_job_id = str(uuid4())
    await store_upload_file_id(arq_pool, upload_job_id, video.file_id)

    keyboard = build_video_action_keyboard(upload_job_id, translator, source="upload")
    await message.answer(translator.t("choose_video_action"), reply_markup=keyboard)


@router.callback_query(lambda c: c.data is not None and matches_prefix(c.data, VideoActionCallback.PREFIX))
@flags.rate_limit("convert")
async def on_video_action_selected(
    callback: CallbackQuery,
    session: AsyncSession,
    translator: Translator,
    arq_pool: ArqRedis,
) -> None:
    try:
        data = VideoActionCallback.unpack(callback.data)
    except CallbackDataError:
        await callback.answer()
        return

    if data.source == "upload":
        file_id = await load_upload_file_id(arq_pool, data.ref_id)
        if file_id is None:
            await callback.answer(translator.t("error_content_not_found"), show_alert=True)
            return
    else:
        probe = await load_probe_result(arq_pool, data.ref_id)
        if probe is None:
            await callback.answer(translator.t("error_content_not_found"), show_alert=True)
            return
        video_formats = [f for f in probe.formats if f.media_type.value == "video"]
        if not video_formats:
            await callback.answer(translator.t("error_content_not_found"), show_alert=True)
            return
        file_id = None  # not applicable for the link path

    await callback.answer()
    if callback.message is None:
        return

    progress_key = "recognizing_in_progress" if data.action == "identify" else (
        "converting_in_progress" if data.action == "audio" else "downloading_in_progress"
    )
    await callback.message.edit_text(translator.t(progress_key))

    job_id = str(uuid4())
    job_type = {
        "identify": JobType.RECOGNIZE.value,
        "audio": JobType.CONVERT.value,
        "video": JobType.DOWNLOAD.value,
    }[data.action]
    await JobRepository.create(session, job_id=job_id, user_id=callback.from_user.id, job_type=job_type)

    common_kwargs = {
        "job_id": job_id,
        "user_id": callback.from_user.id,
        "chat_id": callback.message.chat.id,
        "message_id": callback.message.message_id,
    }

    if data.action == "identify":
        # recognize_job supports the same "upload or link" duality as
        # convert_job (see app/workers/tasks.py), so this branch is
        # symmetric with "audio" below regardless of source.
        if data.source == "upload":
            await arq_pool.enqueue_job("recognize_job", source_file_id=file_id, **common_kwargs)
        else:
            await arq_pool.enqueue_job(
                "recognize_job", probe_job_id=data.ref_id, format_id=video_formats[0].format_id, **common_kwargs
            )
    elif data.action == "audio":
        if data.source == "upload":
            await arq_pool.enqueue_job("convert_job", source_file_id=file_id, **common_kwargs)
        else:
            await arq_pool.enqueue_job(
                "convert_job", probe_job_id=data.ref_id, format_id=video_formats[0].format_id, **common_kwargs
            )
    else:  # "video" — download original, link-only (see build_video_action_keyboard)
        await arq_pool.enqueue_job(
            "download_job", probe_job_id=data.ref_id, format_id=video_formats[0].format_id, **common_kwargs
        )
