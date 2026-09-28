"""Music recognition handler: accepts a voice message, audio file, or video
(as an upload) and enqueues a `recognize_job`. See ARCHITECTURE.md §4.1.

Content-type routing, not FSM: any voice/audio/video message triggers this,
regardless of whether the user tapped "Find Music" first — matching the
spec's "just send me a link or a voice clip" flexibility (see also
handlers/download.py and handlers/convert.py, which route on URL-in-text and
are mutually exclusive with this file's content types).
"""

from __future__ import annotations

from uuid import uuid4

from aiogram import F, Router, flags
from aiogram.types import Audio, Message, Voice
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.menu import MenuButtonFilter
from app.config import Settings
from app.constants import JobType
from app.db.repositories import JobRepository
from app.i18n.translator import Translator

router = Router(name="recognize")

# NOTE: video uploads are deliberately NOT handled here, even though a video
# can also be identified. A video upload gets the explicit 3-way choice
# (identify song / extract audio / nothing else needed, since the user
# already has the original) via handlers/convert.py + VideoActionCallback —
# silently auto-recognizing every uploaded video would remove that choice.
# Voice/audio messages have no such ambiguity: recognition is their only
# sensible action, so they're handled directly here.


@router.message(MenuButtonFilter("menu_find_music"))
async def prompt_find_music(message: Message, translator: Translator) -> None:
    await message.answer(translator.t("ask_send_audio_for_recognition"))


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
