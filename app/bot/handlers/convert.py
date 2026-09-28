"""Uploaded-video handler (ARCHITECTURE.md §4.3).

Automatic UX: when a user uploads a video file, the bot AUTOMATICALLY tries to
identify its music (no menu, no "what would you like to do?" prompt) and, on
the same result, offers an "Extract MP3" button. If no music is found it says
so gracefully and still offers MP3 extraction.

This is implemented by stashing the uploaded video's file_id in the result
cache and enqueuing a `recognize_job` that carries the result token; the
worker attaches the "Extract MP3" action button to whatever it sends back (a
match, or a no-match message). Tapping "Extract MP3" reuses that same file_id
via `convert_job` — the source is never refetched.

Social-media video LINKS are handled entirely by handlers/download.py's
automatic flow (probe -> download -> deliver with its own result-action
buttons), so this file only deals with direct uploads.

The "Extract MP3" / "Find this song" buttons this flow attaches use the same
`ResultActionCallback` as the download flow and are ALL handled by
handlers/download.py's single `on_result_action` (one owner for that callback
prefix, dispatching on the cached context's `platform`), so there is no
callback handler here — only the upload entry point.
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import Message
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.result_cache import ResultActionContext, store_result_context
from app.bot.search_cache import new_token
from app.config import Settings
from app.constants import JobType
from app.db.repositories import JobRepository
from app.i18n.translator import Translator

router = Router(name="convert")


@router.message(F.video)
@flags.rate_limit("recognize")
async def handle_video_upload(
    message: Message,
    session: AsyncSession,
    translator: Translator,
    settings: Settings,
    arq_pool: ArqRedis,
) -> None:
    video = message.video
    if video is None:  # pragma: no cover - filter guarantees this is set
        return

    if video.file_size and video.file_size > settings.MAX_TELEGRAM_FETCH_MB * 1024 * 1024:
        await message.answer(translator.t("error_file_too_big_to_fetch", limit_mb=settings.MAX_TELEGRAM_FETCH_MB))
        return

    # Stash the uploaded file_id so the "Extract MP3" button under the
    # recognition result can reuse it without a re-upload.
    token = new_token()
    await store_result_context(
        arq_pool,
        token,
        ResultActionContext(
            user_id=message.from_user.id,
            file_id=video.file_id,
            source_url="",
            platform="upload",
            probe_job_id="",
        ),
    )

    progress_message = await message.answer(translator.t("video_identifying_music"))

    job_id = str(uuid4())
    await JobRepository.create(session, job_id=job_id, user_id=message.from_user.id, job_type=JobType.RECOGNIZE.value)

    await arq_pool.enqueue_job(
        "recognize_job",
        job_id=job_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=progress_message.message_id,
        source_file_id=video.file_id,
        result_token=token,  # -> worker attaches an "Extract MP3" button
    )
