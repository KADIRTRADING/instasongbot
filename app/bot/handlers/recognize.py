"""Music recognition handler: accepts a voice message or audio file and
enqueues a `recognize_job`. See ARCHITECTURE.md §4.1.

Fully automatic content routing, no menu: any voice/audio message triggers
recognition directly (see also handlers/download.py for URL-in-text
auto-download, handlers/convert.py for uploaded-video auto-recognition, and
handlers/search.py for plain-text music search — all mutually exclusive by
content type).
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import Audio, Message, Voice
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import JobType
from app.db.repositories import JobRepository
from app.i18n.translator import Translator

router = Router(name="recognize")

# NOTE: video uploads are handled in handlers/convert.py, not here. In the
# automatic UX an uploaded video is auto-recognized AND offered an "Extract
# MP3" action in one step (see convert.py's handle_video_upload), so it needs
# the result-action keyboard machinery that lives alongside the convert flow.
# Voice/audio messages have no such follow-up ambiguity — recognition is their
# only sensible action — so they're handled directly here.


@router.message(F.voice | F.audio)
@flags.rate_limit("recognize")
async def handle_recognizable_media(
    message: Message,
    session: AsyncSession,
    translator: Translator,
    settings: Settings,
    arq_pool: ArqRedis,
) -> None:
    media: Voice | Audio | None = message.voice or message.audio
    if media is None:  # pragma: no cover - filter guarantees one is set
        return

    if media.file_size and media.file_size > settings.MAX_TELEGRAM_FETCH_MB * 1024 * 1024:
        await message.answer(translator.t("error_file_too_big_to_fetch", limit_mb=settings.MAX_TELEGRAM_FETCH_MB))
        return

    progress_message = await message.answer(translator.t("recognizing_in_progress"))

    job_id = str(uuid4())
    await JobRepository.create(session, job_id=job_id, user_id=message.from_user.id, job_type=JobType.RECOGNIZE.value)

    await arq_pool.enqueue_job(
        "recognize_job",
        job_id=job_id,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_id=progress_message.message_id,
        source_file_id=media.file_id,
    )
