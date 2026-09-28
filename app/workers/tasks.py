"""arq task functions: the actual work each background job performs.

Every task follows the same shape: look up the job/user, do the slow work
inside a `TempJobDir`, send the result via the worker's own `Bot` instance,
and record the outcome in the `jobs` table — success or failure, always.
Jobs never raise uncaught exceptions past their own boundary: every failure
path is caught, classified, translated, sent to the user as a clear message,
and recorded via `JobRepository.mark_failed`. arq itself will retry a job
that raises (see ARCHITECTURE.md's "jobs may run more than once" note in
worker_settings docs) — but a *user-facing* failure (private content, no
match, file too large) is not something retrying fixes, so we catch those
ourselves and never let them propagate into arq's retry machinery.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile

from app.constants import MediaType
from app.db.repositories import (
    BotSettingRepository,
    BroadcastRepository,
    CaptionRepository,
    JobRepository,
    PlatformRepository,
    UserRepository,
)
from app.i18n.translator import get_translator
from app.logging_conf import get_logger
from app.services.captions.renderer import CaptionContext, CaptionRenderer
from app.services.downloader.errors import (
    ContentNotFoundError,
    DownloaderError,
    DownloadFailedError,
    DownloadTimeoutError,
    FileTooLargeError,
    PrivateContentError,
    RateLimitedError,
    UnsafeURLError,
    UnsupportedURLError,
)
from app.services.media.errors import FFmpegError, MediaValidationError
from app.services.media.tempfiles import TempJobDir
from app.services.recognition.base import RecognitionProviderError
from app.workers.context import WorkerContext

logger = get_logger(__name__)

# Maps a downloader/media exception type to the i18n key used to explain it
# to the user. Order matters only in that DownloaderError/FFmpegError must be
# checked as a fallback after their specific subclasses.
_ERROR_KEY_MAP: tuple[tuple[type[Exception], str], ...] = (
    (PrivateContentError, "error_private_content"),
    (ContentNotFoundError, "error_content_not_found"),
    (RateLimitedError, "error_rate_limited"),
    (UnsafeURLError, "error_unsafe_url"),
    (UnsupportedURLError, "error_unsupported_platform"),
    (DownloadTimeoutError, "error_download_timeout"),
    (FileTooLargeError, "error_file_too_large"),
    (DownloadFailedError, "error_download_failed"),
    (DownloaderError, "error_generic"),
    (MediaValidationError, "error_media_invalid"),
    (FFmpegError, "error_ffmpeg"),
)


def _error_message(translator, exc: Exception) -> str:
    for exc_type, key in _ERROR_KEY_MAP:
        if isinstance(exc, exc_type):
            if isinstance(exc, FileTooLargeError):
                return translator.t(key, size_mb=exc.size_mb, limit_mb=exc.limit_mb)
            return translator.t(key)
    return translator.t("error_generic")


def _friendly_platform_name(translator, platform: str) -> str:
    """Map a raw platform key (e.g. "pinterest") to its translated display
    name (e.g. "Pinterest") via the platform_* i18n keys. Falls back to the
    raw value, titlecased, if the platform isn't one we have a key for."""
    key = f"platform_{platform}"
    translated = translator.t(key)
    return translated if translated != key else platform.title()


async def _get_user_translator(ctx: WorkerContext, user_id: int):
    async with ctx.sessionmaker() as session:
        user = await UserRepository.get(session, user_id)
    language = user.language_code if user else ctx.settings.DEFAULT_LANGUAGE
    return get_translator(language)


async def _resolve_max_download_bytes(ctx: WorkerContext, platform: str | None) -> int:
    """Admin-configurable download size ceiling (§4.5/§6): a per-platform
    override (`PlatformRepository.set_max_file_size_mb`) wins if set, else the
    admin-editable global override (`bot_settings["max_download_mb"]`, see
    handlers/admin.py's "limits" flow) wins if set, else the env-sourced
    `Settings.MAX_DOWNLOAD_MB` cold default. Without this lookup the admin
    "File Size Limits" menu would silently do nothing — nothing else in the
    codebase reads either of those two settings.
    """
    async with ctx.sessionmaker() as session:
        if platform:
            per_platform = await PlatformRepository.get_max_file_size_mb(session, platform)
            if per_platform is not None:
                return per_platform * 1024 * 1024
        global_override = await BotSettingRepository.get(session, "max_download_mb", default=None)
    if global_override is not None:
        return int(global_override) * 1024 * 1024
    return ctx.settings.MAX_DOWNLOAD_MB * 1024 * 1024


async def _render_and_get_buttons(ctx: WorkerContext, media_type: MediaType, context: CaptionContext):
    async with ctx.sessionmaker() as session:
        template = await CaptionRepository.get_template(session, media_type.value)
        button_rows = await CaptionRepository.list_buttons(session, media_type.value)
    buttons = [(b.label, b.url) for b in button_rows]
    return CaptionRenderer.render(template, context, buttons)


async def recognize_job(
    ctx_dict: dict[str, Any],
    *,
    job_id: str,
    user_id: int,
    chat_id: int,
    message_id: int,
    source_file_id: str | None = None,
    probe_job_id: str | None = None,
    format_id: str | None = None,
) -> None:
    """Identify the song in either a Telegram-hosted upload (`source_file_id`)
    or a previously-probed link (`probe_job_id` + `format_id`) — the same
    "upload or link" duality `convert_job` already supports, so "identify the
    song" behaves consistently regardless of which of the two the video came
    from (see handlers/convert.py, which is the only caller that uses the
    link path; handlers/recognize.py only ever uses the upload path).
    """
    ctx: WorkerContext = ctx_dict["worker_ctx"]
    translator = await _get_user_translator(ctx, user_id)

    async with ctx.sessionmaker() as session:
        await JobRepository.mark_processing(session, job_id)
        await session.commit()

    try:
        async with TempJobDir(ctx.settings.WORKDIR, job_id) as job_dir:
            if source_file_id:
                source_path = job_dir / "source"
                await ctx.bot.download(source_file_id, destination=source_path)
            else:
                from app.bot.probe_cache import load_probe_result

                probe_for_source = await load_probe_result(ctx.redis, probe_job_id)
                if probe_for_source is None:
                    raise ContentNotFoundError("This link expired, please send it again")
                max_bytes = await _resolve_max_download_bytes(ctx, probe_for_source.platform)
                downloaded = await ctx.download_manager.download(probe_for_source, format_id, job_dir, max_bytes=max_bytes)
                source_path = downloaded.path

            probe = await ctx.media_tools.probe(source_path)
            clip_path = await ctx.media_tools.extract_recognition_clip(
                source_path, job_dir, clip_seconds=min(ctx.settings.RECOGNITION_CLIP_SECONDS, int(probe.duration_seconds) or 1)
            )

            result = await ctx.recognition_provider.identify(clip_path)

            if not result.matched:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=translator.t("recognition_no_match")
                )
                async with ctx.sessionmaker() as session:
                    await JobRepository.mark_completed(session, job_id, {"matched": False})
                    await session.commit()
                return

            lines = [f"🎵 <b>{result.title}</b>"]
            if result.artist:
                lines.append(f"👤 {result.artist}")
            if result.album:
                lines.append(f"💿 {result.album}")
            if result.links:
                lines.append("")
                lines.append(" · ".join(f'<a href="{link.url}">{link.platform}</a>' for link in result.links))
            text = "\n".join(lines)
            if result.is_low_confidence:
                text = translator.t("recognition_low_confidence_prefix") + text

            if result.cover_art_url:
                await ctx.bot.send_photo(chat_id=chat_id, photo=result.cover_art_url, caption=text)
                await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
            else:
                await ctx.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)

            async with ctx.sessionmaker() as session:
                await JobRepository.mark_completed(
                    session, job_id, {"matched": True, "title": result.title, "artist": result.artist}
                )
                await session.commit()

    except RecognitionProviderError as exc:
        logger.warning("recognize_job_provider_error", job_id=job_id, error=str(exc))
        await _fail_job(ctx, job_id, chat_id, message_id, translator.t("recognition_error"), str(exc))
    except MediaValidationError as exc:
        logger.warning("recognize_job_invalid_media", job_id=job_id, error=str(exc))
        await _fail_job(ctx, job_id, chat_id, message_id, translator.t("error_media_invalid"), str(exc))
    except Exception as exc:  # noqa: BLE001 - top-level job boundary, see module docstring
        logger.error("recognize_job_unexpected_error", job_id=job_id, error=str(exc), exc_info=True)
        await _fail_job(ctx, job_id, chat_id, message_id, translator.t("error_generic"), str(exc))


async def probe_job(ctx_dict: dict[str, Any], *, job_id: str, user_id: int, chat_id: int, message_id: int, url: str) -> None:
    """Metadata-only probe of a social-media link; results are sent back as an
    inline keyboard built by the bot layer's callback handlers (see
    app/bot/handlers/download.py), not here — this task's job is purely to
    resolve `ProbeResult` and hand it to the presentation layer via a stashed
    Redis value keyed by job_id (short TTL), since arq job results aren't
    designed to be polled by a chat handler directly.
    """
    ctx: WorkerContext = ctx_dict["worker_ctx"]
    translator = await _get_user_translator(ctx, user_id)

    async with ctx.sessionmaker() as session:
        await JobRepository.mark_processing(session, job_id)
        await session.commit()

    try:
        probe = await ctx.download_manager.probe(url)

        async with ctx.sessionmaker() as session:
            enabled = await PlatformRepository.is_enabled(session, probe.platform)
        if not enabled:
            raise UnsupportedURLError(f"{probe.platform} is disabled by admin")

        from app.bot.probe_cache import store_probe_result

        await store_probe_result(ctx.redis, job_id, probe)

        has_video = any(f.media_type == MediaType.VIDEO for f in probe.formats)

        if has_video:
            # A link that resolves to video gets the same 3-way choice as an
            # uploaded video (identify song / extract audio / download
            # original) — see ARCHITECTURE.md §4.3 and
            # app/bot/handlers/convert.py, which owns the VideoActionCallback
            # this keyboard produces.
            from app.bot.keyboards.download import build_video_action_keyboard

            keyboard = build_video_action_keyboard(job_id, translator, source="link")
            text = translator.t("choose_video_action")
        else:
            from app.bot.keyboards.download import build_format_keyboard

            keyboard = build_format_keyboard(job_id, probe, translator)
            text = (
                translator.t("choose_download_option_carousel", count=len(probe.formats))
                if len(probe.formats) > 1 and all(f.media_type == MediaType.IMAGE for f in probe.formats)
                else translator.t("choose_download_option")
            )
        await ctx.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard)

        async with ctx.sessionmaker() as session:
            await JobRepository.mark_completed(session, job_id, {"platform": probe.platform})
            await session.commit()

    except Exception as exc:  # noqa: BLE001 - top-level job boundary, see module docstring
        translated = _error_message(translator, exc) if isinstance(exc, DownloaderError) else translator.t("error_generic")
        level = "warning" if isinstance(exc, DownloaderError) else "error"
        getattr(logger, level)("probe_job_failed", job_id=job_id, error=str(exc), exc_info=level == "error")
        await _fail_job(ctx, job_id, chat_id, message_id, translated, str(exc))


async def download_job(
    ctx_dict: dict[str, Any],
    *,
    job_id: str,
    user_id: int,
    chat_id: int,
    message_id: int,
    probe_job_id: str,
    format_id: str,
) -> None:
    """Fetch the bytes for a previously-probed format and deliver them."""
    ctx: WorkerContext = ctx_dict["worker_ctx"]
    translator = await _get_user_translator(ctx, user_id)

    async with ctx.sessionmaker() as session:
        await JobRepository.mark_processing(session, job_id)
        await session.commit()

    try:
        from app.bot.probe_cache import load_probe_result

        probe = await load_probe_result(ctx.redis, probe_job_id)
        if probe is None:
            raise ContentNotFoundError("This link expired, please send it again")

        async with TempJobDir(ctx.settings.WORKDIR, job_id) as job_dir:
            max_bytes = await _resolve_max_download_bytes(ctx, probe.platform)
            downloaded = await ctx.download_manager.download(probe, format_id, job_dir, max_bytes=max_bytes)
            source_name = _friendly_platform_name(translator, probe.platform)
            await _deliver_file(ctx, chat_id, message_id, downloaded, source=source_name, translator=translator)

        async with ctx.sessionmaker() as session:
            await JobRepository.mark_completed(session, job_id, {"platform": probe.platform, "format_id": format_id})
            await session.commit()

    except DownloaderError as exc:
        logger.warning("download_job_failed", job_id=job_id, error=str(exc))
        await _fail_job(ctx, job_id, chat_id, message_id, _error_message(translator, exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("download_job_unexpected_error", job_id=job_id, error=str(exc), exc_info=True)
        await _fail_job(ctx, job_id, chat_id, message_id, translator.t("error_generic"), str(exc))


async def convert_job(
    ctx_dict: dict[str, Any],
    *,
    job_id: str,
    user_id: int,
    chat_id: int,
    message_id: int,
    source_file_id: str | None = None,
    probe_job_id: str | None = None,
    format_id: str | None = None,
) -> None:
    """Video -> MP3 extraction, for either an uploaded file (source_file_id)
    or a previously-probed link (probe_job_id + format_id)."""
    ctx: WorkerContext = ctx_dict["worker_ctx"]
    translator = await _get_user_translator(ctx, user_id)

    async with ctx.sessionmaker() as session:
        await JobRepository.mark_processing(session, job_id)
        await session.commit()

    try:
        async with TempJobDir(ctx.settings.WORKDIR, job_id) as job_dir:
            if source_file_id:
                source_path = job_dir / "source"
                await ctx.bot.download(source_file_id, destination=source_path)
                source_title = None
            else:
                from app.bot.probe_cache import load_probe_result

                probe = await load_probe_result(ctx.redis, probe_job_id)
                if probe is None:
                    raise ContentNotFoundError("This link expired, please send it again")
                max_bytes = await _resolve_max_download_bytes(ctx, probe.platform)
                downloaded = await ctx.download_manager.download(probe, format_id, job_dir, max_bytes=max_bytes)
                source_path = downloaded.path
                source_title = downloaded.title

            audio_path = await ctx.media_tools.extract_audio(source_path, job_dir, ext="mp3")
            downloaded_audio = _to_downloaded_file(audio_path, MediaType.AUDIO, source_title)
            await _deliver_file(ctx, chat_id, message_id, downloaded_audio, source="", translator=translator)

        async with ctx.sessionmaker() as session:
            await JobRepository.mark_completed(session, job_id)
            await session.commit()

    except DownloaderError as exc:
        logger.warning("convert_job_download_failed", job_id=job_id, error=str(exc))
        await _fail_job(ctx, job_id, chat_id, message_id, _error_message(translator, exc), str(exc))
    except (MediaValidationError, FFmpegError) as exc:
        logger.warning("convert_job_media_failed", job_id=job_id, error=str(exc))
        await _fail_job(ctx, job_id, chat_id, message_id, _error_message(translator, exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.error("convert_job_unexpected_error", job_id=job_id, error=str(exc), exc_info=True)
        await _fail_job(ctx, job_id, chat_id, message_id, translator.t("error_generic"), str(exc))


def _to_downloaded_file(path: Path, media_type: MediaType, title: str | None):
    from app.services.downloader.models import DownloadedFile

    return DownloadedFile(
        path=path, media_type=media_type, title=title, uploader=None, ext=path.suffix.lstrip("."), size_bytes=path.stat().st_size
    )


async def _deliver_file(ctx: WorkerContext, chat_id: int, message_id: int, downloaded, *, source: str, translator) -> None:
    """Send the file directly if it fits Telegram's upload ceiling, otherwise
    upload to storage and send a secure temporary link instead."""
    size_mb = downloaded.size_bytes / (1024 * 1024)
    context = CaptionContext(
        title=downloaded.title or "", artist="", source=source, bot_username=ctx.settings.BOT_USERNAME
    )
    rendered = await _render_and_get_buttons(ctx, downloaded.media_type, context)

    if size_mb <= ctx.settings.TELEGRAM_DIRECT_UPLOAD_MB:
        input_file = FSInputFile(downloaded.path)
        if downloaded.media_type == MediaType.VIDEO:
            await ctx.bot.send_video(
                chat_id=chat_id, video=input_file, caption=rendered.text, reply_markup=rendered.reply_markup
            )
        elif downloaded.media_type == MediaType.AUDIO:
            await ctx.bot.send_audio(
                chat_id=chat_id, audio=input_file, caption=rendered.text, reply_markup=rendered.reply_markup
            )
        else:
            await ctx.bot.send_photo(
                chat_id=chat_id, photo=input_file, caption=rendered.text, reply_markup=rendered.reply_markup
            )
        await ctx.bot.delete_message(chat_id=chat_id, message_id=message_id)
    else:
        stored = await ctx.storage_backend.upload(
            downloaded.path, filename=downloaded.path.name, content_type=""
        )
        link_text = translator.t(
            "sending_large_file_link", minutes=stored.expires_in_seconds // 60, url=stored.url
        )
        await ctx.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"{link_text}\n\n{rendered.text}")


async def _fail_job(ctx: WorkerContext, job_id: str, chat_id: int, message_id: int, user_message: str, error_detail: str) -> None:
    try:
        await ctx.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=user_message)
    except Exception:  # noqa: BLE001 - the original message may have been deleted/edited already
        logger.warning("fail_job_could_not_edit_message", job_id=job_id, chat_id=chat_id)

    async with ctx.sessionmaker() as session:
        await JobRepository.mark_failed(session, job_id, error_detail)
        await session.commit()


async def broadcast_job(ctx_dict: dict[str, Any], *, broadcast_id: str) -> None:
    """Send an admin's announcement to every non-banned user (ARCHITECTURE.md
    §6). Runs as its own background job specifically so a broadcast to
    thousands of users can't block anything else — and so its own progress
    (sent/failed counts) is observable in the `broadcasts` table rather than
    being a fire-and-forget black box, per the admin-controls requirement.

    A per-recipient failure (blocked bot, deactivated account, deleted chat)
    is expected at scale and does not fail the whole broadcast — we count it
    and move on. A `TelegramRetryAfter` (flood control) is honored by
    actually sleeping for the requested duration before continuing, since
    ignoring it would just get every subsequent send rate-limited too.
    """
    ctx: WorkerContext = ctx_dict["worker_ctx"]

    async with ctx.sessionmaker() as session:
        broadcast = await BroadcastRepository.get(session, broadcast_id)
        if broadcast is None:
            logger.error("broadcast_job_not_found", broadcast_id=broadcast_id)
            return
        user_ids = await UserRepository.iter_all_ids(session, exclude_banned=True)
        await BroadcastRepository.update_progress(session, broadcast_id, total_users=len(user_ids), status="sending")
        await session.commit()
        message_text = broadcast.message_text

    sent_count = 0
    failed_count = 0

    for user_id in user_ids:
        try:
            await ctx.bot.send_message(chat_id=user_id, text=message_text)
            sent_count += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            try:
                await ctx.bot.send_message(chat_id=user_id, text=message_text)
                sent_count += 1
            except Exception as retry_exc:  # noqa: BLE001 - one recipient's failure must not abort the whole broadcast
                logger.warning("broadcast_send_failed_after_retry", user_id=user_id, error=str(retry_exc))
                failed_count += 1
        except TelegramForbiddenError:
            # User blocked the bot or deleted their account — expected at
            # scale, not worth logging individually.
            failed_count += 1
        except Exception as exc:  # noqa: BLE001 - see broadcast_job docstring
            logger.warning("broadcast_send_failed", user_id=user_id, error=str(exc))
            failed_count += 1

        if (sent_count + failed_count) % 50 == 0:
            async with ctx.sessionmaker() as session:
                await BroadcastRepository.update_progress(session, broadcast_id, sent_count=sent_count, failed_count=failed_count)
                await session.commit()

    async with ctx.sessionmaker() as session:
        await BroadcastRepository.update_progress(
            session,
            broadcast_id,
            sent_count=sent_count,
            failed_count=failed_count,
            status="completed",
            completed_at=datetime.now(UTC),
        )
        await session.commit()

    logger.info("broadcast_job_completed", broadcast_id=broadcast_id, sent=sent_count, failed=failed_count)
