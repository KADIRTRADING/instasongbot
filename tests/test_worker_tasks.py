"""Tests for app/workers/tasks.py's recognize_job/probe_job/download_job/
convert_job and their shared helpers (_resolve_max_download_bytes,
_error_message, _deliver_file, _fail_job).

Follows the same pattern as tests/test_broadcast_job.py (which already
covers broadcast_job): a REAL in-memory SQLite DB via the app's own session
module, a REAL fakeredis instance for the probe/upload caches, and a REAL
MediaTools/DownloadManager/CaptionRenderer call graph -- only the network
edges (yt-dlp/Pinterest HTTP calls, the recognition provider's HTTP call)
and the Telegram Bot itself are mocked, since those are the only parts that
would need real external services or real API keys to exercise for real
(already covered by the opt-in `-m live` tests elsewhere in this suite).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot.probe_cache import store_probe_result
from app.config import Settings
from app.constants import MediaType
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import JobRepository, PlatformRepository, UserRepository
from app.services.downloader.errors import (
    ContentNotFoundError,
    FileTooLargeError,
    PrivateContentError,
)
from app.services.downloader.models import DownloadedFile, MediaFormat, ProbeResult
from app.services.media.errors import MediaValidationError
from app.services.recognition.base import RecognitionProviderError, RecognitionResult
from app.workers.context import WorkerContext
from app.workers.tasks import (
    _resolve_max_download_bytes,
    convert_job,
    download_job,
    probe_job,
    recognize_job,
)


@pytest.fixture(autouse=True)
async def reset_db_engine():
    await db_session_module.dispose_engine()
    yield
    await db_session_module.dispose_engine()


@pytest.fixture
async def db_engine():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_session_module._engine = engine
    db_session_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield engine
    await engine.dispose()


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        RECOGNITION_PROVIDER="audd",
        AUDD_API_TOKEN="test",
        STORAGE_BACKEND="local",
        PUBLIC_BASE_URL="http://localhost:8080",
        WORKDIR=str(tmp_path),
        MAX_DOWNLOAD_MB=500,
        TELEGRAM_DIRECT_UPLOAD_MB=50,
    )


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock()

    async def fake_edit_message_text(chat_id, message_id, text, reply_markup=None):
        return True

    bot.edit_message_text = AsyncMock(side_effect=fake_edit_message_text)
    return bot


@pytest.fixture
def worker_ctx(settings: Settings, fake_redis, mock_bot: AsyncMock) -> WorkerContext:
    from app.services.downloader.manager import DownloadManager
    from app.services.media.ffmpeg_tools import MediaTools
    from app.services.recognition.factory import get_recognition_provider
    from app.services.storage.factory import get_storage_backend

    return WorkerContext(
        settings=settings,
        bot=mock_bot,
        sessionmaker=db_session_module.get_sessionmaker(),
        redis=fake_redis,
        recognition_provider=get_recognition_provider(settings),
        download_manager=DownloadManager(settings),
        media_tools=MediaTools(timeout_seconds=60),
        storage_backend=get_storage_backend(settings),
    )


async def _seed_user(user_id: int = 1, language: str = "en") -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await UserRepository.get_or_create(session, user_id=user_id, username=None, first_name=None, default_language=language)
        await session.commit()


async def _seed_job(job_id: str, user_id: int, job_type: str, platform: str | None = None) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await JobRepository.create(session, job_id=job_id, user_id=user_id, job_type=job_type, platform=platform)
        await session.commit()


def _fake_video_probe(n_formats: int = 1) -> ProbeResult:
    formats = tuple(
        MediaFormat(format_id=f"video:{i}", media_type=MediaType.VIDEO, label=f"{720 - i * 100}p", ext="mp4")
        for i in range(n_formats)
    )
    return ProbeResult(
        platform="youtube",
        source_url="https://www.youtube.com/watch?v=abc123",
        title="Test Video",
        uploader="uploader1",
        thumbnail_url=None,
        duration_seconds=60.0,
        formats=formats,
    )


def _fake_image_probe(n_images: int = 1) -> ProbeResult:
    formats = tuple(
        MediaFormat(format_id=f"image:{i}", media_type=MediaType.IMAGE, label=f"Image {i + 1}", ext="jpg")
        for i in range(n_images)
    )
    return ProbeResult(
        platform="pinterest",
        source_url="https://www.pinterest.com/pin/123/",
        title="Test Pin",
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=formats,
    )


# --- _resolve_max_download_bytes --------------------------------------------


async def test_resolve_max_bytes_falls_back_to_env_default(db_engine, worker_ctx) -> None:
    result = await _resolve_max_download_bytes(worker_ctx, "youtube")
    assert result == 500 * 1024 * 1024  # Settings.MAX_DOWNLOAD_MB from the fixture


async def test_resolve_max_bytes_uses_global_bot_setting_override(db_engine, worker_ctx) -> None:
    from app.db.repositories import BotSettingRepository

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await BotSettingRepository.set(session, "max_download_mb", 100, updated_by=1)
        await session.commit()

    result = await _resolve_max_download_bytes(worker_ctx, "youtube")
    assert result == 100 * 1024 * 1024


async def test_resolve_max_bytes_per_platform_override_wins_over_global(db_engine, worker_ctx) -> None:
    from app.db.repositories import BotSettingRepository

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await BotSettingRepository.set(session, "max_download_mb", 100, updated_by=1)
        await PlatformRepository.set_max_file_size_mb(session, "youtube", 50)
        await session.commit()

    yt_result = await _resolve_max_download_bytes(worker_ctx, "youtube")
    other_result = await _resolve_max_download_bytes(worker_ctx, "tiktok")

    assert yt_result == 50 * 1024 * 1024  # per-platform override wins
    assert other_result == 100 * 1024 * 1024  # falls through to global override


async def test_resolve_max_bytes_with_no_platform_uses_global_or_default(db_engine, worker_ctx) -> None:
    result = await _resolve_max_download_bytes(worker_ctx, None)
    assert result == 500 * 1024 * 1024


# --- recognize_job -----------------------------------------------------------


async def test_recognize_job_upload_no_match_edits_message(db_engine, worker_ctx, mock_bot, tmp_path, monkeypatch) -> None:
    await _seed_user(1)
    await _seed_job("job-1", 1, "recognize")

    async def fake_download(file_id, destination):
        Path(destination).write_bytes(b"fake audio bytes")

    mock_bot.download = AsyncMock(side_effect=fake_download)

    async def fake_probe(path):
        from app.services.media.ffmpeg_tools import MediaProbe

        return MediaProbe(duration_seconds=10.0, has_video=False, has_audio=True, format_name="mp3", size_bytes=100)

    monkeypatch.setattr(worker_ctx.media_tools, "probe", fake_probe)

    async def fake_extract_clip(source, job_dir, clip_seconds):
        clip_path = job_dir / "clip.mp3"
        clip_path.write_bytes(b"clip bytes")
        return clip_path

    monkeypatch.setattr(worker_ctx.media_tools, "extract_recognition_clip", fake_extract_clip)
    monkeypatch.setattr(worker_ctx.recognition_provider, "identify", AsyncMock(return_value=RecognitionResult(matched=False)))

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-1", user_id=1, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    mock_bot.edit_message_text.assert_called_once()
    _, kwargs = mock_bot.edit_message_text.call_args
    assert kwargs["chat_id"] == 999
    assert kwargs["message_id"] == 5
    assert "identify" in kwargs["text"].lower() or "😕" in kwargs["text"]

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-1")
        assert job.status == "completed"
        assert job.result_meta == {"matched": False}


async def test_recognize_job_upload_match_sends_photo_with_cover_art(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(2)
    await _seed_job("job-2", 2, "recognize")

    async def fake_download(file_id, destination):
        Path(destination).write_bytes(b"fake audio bytes")

    mock_bot.download = AsyncMock(side_effect=fake_download)

    async def fake_probe(path):
        from app.services.media.ffmpeg_tools import MediaProbe

        return MediaProbe(duration_seconds=10.0, has_video=False, has_audio=True, format_name="mp3", size_bytes=100)

    monkeypatch.setattr(worker_ctx.media_tools, "probe", fake_probe)

    async def fake_extract_clip(source, job_dir, clip_seconds):
        clip_path = job_dir / "clip.mp3"
        clip_path.write_bytes(b"clip bytes")
        return clip_path

    monkeypatch.setattr(worker_ctx.media_tools, "extract_recognition_clip", fake_extract_clip)

    result = RecognitionResult(matched=True, title="Song Title", artist="Artist Name", cover_art_url="https://example.com/cover.jpg")
    monkeypatch.setattr(worker_ctx.recognition_provider, "identify", AsyncMock(return_value=result))

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-2", user_id=2, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    mock_bot.send_photo.assert_called_once()
    _, kwargs = mock_bot.send_photo.call_args
    assert kwargs["chat_id"] == 999
    assert "Song Title" in kwargs["caption"]
    assert "Artist Name" in kwargs["caption"]
    mock_bot.delete_message.assert_called_once_with(chat_id=999, message_id=5)

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-2")
        assert job.status == "completed"
        assert job.result_meta == {"matched": True, "title": "Song Title", "artist": "Artist Name"}


async def test_recognize_job_low_confidence_prefixes_message(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(3)
    await _seed_job("job-3", 3, "recognize")

    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"x"))

    async def fake_probe(path):
        from app.services.media.ffmpeg_tools import MediaProbe

        return MediaProbe(duration_seconds=10.0, has_video=False, has_audio=True, format_name="mp3", size_bytes=100)

    monkeypatch.setattr(worker_ctx.media_tools, "probe", fake_probe)
    monkeypatch.setattr(
        worker_ctx.media_tools,
        "extract_recognition_clip",
        lambda source, job_dir, clip_seconds: _write_and_return(job_dir / "clip.mp3"),
    )

    result = RecognitionResult(matched=True, title="Low Conf Song", artist="X", score=30)  # < 70 -> low confidence
    monkeypatch.setattr(worker_ctx.recognition_provider, "identify", AsyncMock(return_value=result))

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-3", user_id=3, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    mock_bot.edit_message_text.assert_called_once()
    _, kwargs = mock_bot.edit_message_text.call_args
    assert "⚠️" in kwargs["text"]
    assert "Low Conf Song" in kwargs["text"]


async def _write_and_return(path: Path):
    path.write_bytes(b"clip")
    return path


async def test_recognize_job_provider_error_fails_job_with_translated_message(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(4)
    await _seed_job("job-4", 4, "recognize")

    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"x"))

    async def fake_probe(path):
        from app.services.media.ffmpeg_tools import MediaProbe

        return MediaProbe(duration_seconds=10.0, has_video=False, has_audio=True, format_name="mp3", size_bytes=100)

    monkeypatch.setattr(worker_ctx.media_tools, "probe", fake_probe)
    monkeypatch.setattr(
        worker_ctx.media_tools, "extract_recognition_clip", lambda source, job_dir, clip_seconds: _write_and_return(job_dir / "clip.mp3")
    )
    monkeypatch.setattr(
        worker_ctx.recognition_provider, "identify", AsyncMock(side_effect=RecognitionProviderError("provider down"))
    )

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-4", user_id=4, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-4")
        assert job.status == "failed"
        assert "provider down" in job.error_message


async def test_recognize_job_invalid_media_fails_job(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(5)
    await _seed_job("job-5", 5, "recognize")

    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"not media"))
    monkeypatch.setattr(
        worker_ctx.media_tools, "probe", AsyncMock(side_effect=MediaValidationError("not a valid media file"))
    )

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-5", user_id=5, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-5")
        assert job.status == "failed"


async def test_recognize_job_link_source_uses_probe_cache_and_download_manager(
    db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch
) -> None:
    """recognize_job supports the same "upload or link" duality as
    convert_job (see handlers/convert.py) -- this exercises the link path."""
    await _seed_user(6)
    await _seed_job("job-6", 6, "recognize")

    probe = _fake_video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-1", probe)

    async def fake_download(probe_arg, format_id, job_dir, **kwargs):
        path = job_dir / "downloaded.mp4"
        path.write_bytes(b"fake video bytes")
        return DownloadedFile(path=path, media_type=MediaType.VIDEO, title="Test Video", uploader=None, ext="mp4", size_bytes=path.stat().st_size)

    monkeypatch.setattr(worker_ctx.download_manager, "download", fake_download)

    async def fake_probe_media(path):
        from app.services.media.ffmpeg_tools import MediaProbe

        return MediaProbe(duration_seconds=30.0, has_video=True, has_audio=True, format_name="mp4", size_bytes=1000)

    monkeypatch.setattr(worker_ctx.media_tools, "probe", fake_probe_media)
    monkeypatch.setattr(
        worker_ctx.media_tools, "extract_recognition_clip", lambda source, job_dir, clip_seconds: _write_and_return(job_dir / "clip.mp3")
    )
    monkeypatch.setattr(worker_ctx.recognition_provider, "identify", AsyncMock(return_value=RecognitionResult(matched=False)))

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-6", user_id=6, chat_id=999, message_id=5, probe_job_id="probe-1", format_id="video:0"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-6")
        assert job.status == "completed"


async def test_recognize_job_expired_probe_link_fails_gracefully(db_engine, worker_ctx, mock_bot) -> None:
    await _seed_user(7)
    await _seed_job("job-7", 7, "recognize")

    await recognize_job(
        {"worker_ctx": worker_ctx},
        job_id="job-7",
        user_id=7,
        chat_id=999,
        message_id=5,
        probe_job_id="never-existed",
        format_id="video:0",
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-7")
        assert job.status == "failed"


# --- probe_job ----------------------------------------------------------


async def test_probe_job_image_shows_format_keyboard(db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch) -> None:
    await _seed_user(10)
    await _seed_job("job-10", 10, "probe", platform="pinterest")

    probe = _fake_image_probe(n_images=1)
    monkeypatch.setattr(worker_ctx.download_manager, "probe", AsyncMock(return_value=probe))

    await probe_job({"worker_ctx": worker_ctx}, job_id="job-10", user_id=10, chat_id=999, message_id=5, url="https://pinterest.com/pin/1")

    mock_bot.edit_message_text.assert_called_once()
    _, kwargs = mock_bot.edit_message_text.call_args
    assert kwargs["reply_markup"] is not None

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-10")
        assert job.status == "completed"
        assert job.result_meta == {"platform": "pinterest"}

    # Probe result was stashed in redis for the callback handler to retrieve later.
    from app.bot.probe_cache import load_probe_result

    cached = await load_probe_result(fake_redis, "job-10")
    assert cached is not None
    assert cached.platform == "pinterest"


async def test_probe_job_video_shows_video_action_keyboard(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(11)
    await _seed_job("job-11", 11, "probe", platform="youtube")

    probe = _fake_video_probe(n_formats=2)
    monkeypatch.setattr(worker_ctx.download_manager, "probe", AsyncMock(return_value=probe))

    await probe_job({"worker_ctx": worker_ctx}, job_id="job-11", user_id=11, chat_id=999, message_id=5, url="https://youtube.com/watch?v=x")

    _, kwargs = mock_bot.edit_message_text.call_args
    keyboard = kwargs["reply_markup"]
    # 2 rows for upload (identify+audio) would be wrong here -- source="link"
    # gets a 3rd "download original" row too (see build_video_action_keyboard).
    assert len(keyboard.inline_keyboard) == 3


async def test_probe_job_disabled_platform_fails_with_translated_error(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(12)
    await _seed_job("job-12", 12, "probe", platform="tiktok")

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await PlatformRepository.set_enabled(session, "tiktok", False)
        await session.commit()

    probe = _fake_video_probe(n_formats=1)
    probe = ProbeResult(**{**probe.__dict__, "platform": "tiktok"})
    monkeypatch.setattr(worker_ctx.download_manager, "probe", AsyncMock(return_value=probe))

    await probe_job({"worker_ctx": worker_ctx}, job_id="job-12", user_id=12, chat_id=999, message_id=5, url="https://tiktok.com/@x/video/1")

    async with sm() as session:
        job = await JobRepository.get(session, "job-12")
        assert job.status == "failed"

    _, kwargs = mock_bot.edit_message_text.call_args
    assert "❌" in kwargs["text"]


async def test_probe_job_content_not_found_fails_gracefully(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(13)
    await _seed_job("job-13", 13, "probe", platform="youtube")

    monkeypatch.setattr(worker_ctx.download_manager, "probe", AsyncMock(side_effect=ContentNotFoundError("gone")))

    await probe_job({"worker_ctx": worker_ctx}, job_id="job-13", user_id=13, chat_id=999, message_id=5, url="https://youtube.com/watch?v=x")

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-13")
        assert job.status == "failed"
        assert "gone" in job.error_message


# --- download_job -------------------------------------------------------


async def test_download_job_delivers_small_file_directly(db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch, tmp_path) -> None:
    await _seed_user(20)
    await _seed_job("job-20", 20, "download", platform="pinterest")

    probe = _fake_image_probe(n_images=1)
    await store_probe_result(fake_redis, "probe-20", probe)

    async def fake_download(probe_arg, format_id, job_dir, **kwargs):
        path = job_dir / "img.jpg"
        path.write_bytes(b"x" * 1000)
        return DownloadedFile(path=path, media_type=MediaType.IMAGE, title="Pin Title", uploader=None, ext="jpg", size_bytes=1000)

    monkeypatch.setattr(worker_ctx.download_manager, "download", fake_download)

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-20", user_id=20, chat_id=999, message_id=5, probe_job_id="probe-20", format_id="image:0"
    )

    mock_bot.send_photo.assert_called_once()
    mock_bot.delete_message.assert_called_once_with(chat_id=999, message_id=5)

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-20")
        assert job.status == "completed"
        assert job.result_meta == {"platform": "pinterest", "format_id": "image:0"}


async def test_download_job_large_file_uploads_to_storage_and_sends_link(
    db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch, tmp_path
) -> None:
    await _seed_user(21)
    await _seed_job("job-21", 21, "download", platform="pinterest")

    probe = _fake_image_probe(n_images=1)
    await store_probe_result(fake_redis, "probe-21", probe)

    big_size = (worker_ctx.settings.TELEGRAM_DIRECT_UPLOAD_MB + 5) * 1024 * 1024

    async def fake_download(probe_arg, format_id, job_dir, **kwargs):
        path = job_dir / "img.jpg"
        path.write_bytes(b"x" * 100)  # actual bytes small, we lie about size_bytes below
        return DownloadedFile(path=path, media_type=MediaType.IMAGE, title="Pin Title", uploader=None, ext="jpg", size_bytes=big_size)

    monkeypatch.setattr(worker_ctx.download_manager, "download", fake_download)

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-21", user_id=21, chat_id=999, message_id=5, probe_job_id="probe-21", format_id="image:0"
    )

    mock_bot.send_photo.assert_not_called()
    mock_bot.edit_message_text.assert_called_once()
    _, kwargs = mock_bot.edit_message_text.call_args
    assert "http" in kwargs["text"]  # the storage link was included

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-21")
        assert job.status == "completed"


async def test_download_job_expired_probe_fails_gracefully(db_engine, worker_ctx, mock_bot) -> None:
    await _seed_user(22)
    await _seed_job("job-22", 22, "download", platform="pinterest")

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-22", user_id=22, chat_id=999, message_id=5, probe_job_id="nonexistent", format_id="image:0"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-22")
        assert job.status == "failed"


async def test_download_job_file_too_large_translates_size_and_limit(
    db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch
) -> None:
    await _seed_user(23)
    await _seed_job("job-23", 23, "download", platform="youtube")

    probe = _fake_video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-23", probe)

    monkeypatch.setattr(
        worker_ctx.download_manager, "download", AsyncMock(side_effect=FileTooLargeError(size_mb=600.0, limit_mb=500))
    )

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-23", user_id=23, chat_id=999, message_id=5, probe_job_id="probe-23", format_id="video:0"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-23")
        assert job.status == "failed"

    _, kwargs = mock_bot.edit_message_text.call_args
    assert "600" in kwargs["text"]
    assert "500" in kwargs["text"]


async def test_download_job_private_content_error(db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch) -> None:
    await _seed_user(24)
    await _seed_job("job-24", 24, "download", platform="instagram")

    probe = _fake_image_probe(n_images=1)
    probe = ProbeResult(**{**probe.__dict__, "platform": "instagram"})
    await store_probe_result(fake_redis, "probe-24", probe)

    monkeypatch.setattr(worker_ctx.download_manager, "download", AsyncMock(side_effect=PrivateContentError("private account")))

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-24", user_id=24, chat_id=999, message_id=5, probe_job_id="probe-24", format_id="image:0"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-24")
        assert job.status == "failed"
    _, kwargs = mock_bot.edit_message_text.call_args
    assert "🔒" in kwargs["text"]


# --- convert_job ---------------------------------------------------------


async def test_convert_job_upload_extracts_audio_and_delivers(db_engine, worker_ctx, mock_bot, monkeypatch, tmp_path) -> None:
    await _seed_user(30)
    await _seed_job("job-30", 30, "convert")

    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"video bytes"))

    async def fake_extract_audio(source, job_dir, ext="mp3"):
        audio_path = job_dir / "audio.mp3"
        audio_path.write_bytes(b"audio bytes")
        return audio_path

    monkeypatch.setattr(worker_ctx.media_tools, "extract_audio", fake_extract_audio)

    await convert_job({"worker_ctx": worker_ctx}, job_id="job-30", user_id=30, chat_id=999, message_id=5, source_file_id="file-xyz")

    mock_bot.send_audio.assert_called_once()
    mock_bot.delete_message.assert_called_once_with(chat_id=999, message_id=5)

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-30")
        assert job.status == "completed"


async def test_convert_job_link_downloads_then_extracts_audio(db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch) -> None:
    await _seed_user(31)
    await _seed_job("job-31", 31, "convert")

    probe = _fake_video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-31", probe)

    async def fake_download(probe_arg, format_id, job_dir, **kwargs):
        path = job_dir / "video.mp4"
        path.write_bytes(b"video bytes")
        return DownloadedFile(path=path, media_type=MediaType.VIDEO, title="Downloaded Video", uploader=None, ext="mp4", size_bytes=100)

    monkeypatch.setattr(worker_ctx.download_manager, "download", fake_download)

    async def fake_extract_audio(source, job_dir, ext="mp3"):
        audio_path = job_dir / "audio.mp3"
        audio_path.write_bytes(b"audio bytes")
        return audio_path

    monkeypatch.setattr(worker_ctx.media_tools, "extract_audio", fake_extract_audio)

    await convert_job(
        {"worker_ctx": worker_ctx}, job_id="job-31", user_id=31, chat_id=999, message_id=5, probe_job_id="probe-31", format_id="video:0"
    )

    mock_bot.send_audio.assert_called_once()
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-31")
        assert job.status == "completed"


async def test_convert_job_ffmpeg_failure_fails_job(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    from app.services.media.errors import FFmpegError

    await _seed_user(32)
    await _seed_job("job-32", 32, "convert")

    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"video bytes"))
    monkeypatch.setattr(worker_ctx.media_tools, "extract_audio", AsyncMock(side_effect=FFmpegError("ffmpeg crashed")))

    await convert_job({"worker_ctx": worker_ctx}, job_id="job-32", user_id=32, chat_id=999, message_id=5, source_file_id="file-xyz")

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-32")
        assert job.status == "failed"
    _, kwargs = mock_bot.edit_message_text.call_args
    assert "❌" in kwargs["text"]


async def test_convert_job_expired_probe_link_fails_gracefully(db_engine, worker_ctx, mock_bot) -> None:
    await _seed_user(33)
    await _seed_job("job-33", 33, "convert")

    await convert_job(
        {"worker_ctx": worker_ctx}, job_id="job-33", user_id=33, chat_id=999, message_id=5, probe_job_id="nonexistent", format_id="video:0"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-33")
        assert job.status == "failed"


# --- _fail_job's own resilience (edit fails, e.g. original message deleted) --


# --- Additional branch coverage: video delivery, streaming links, generic --
# --- (non-DownloaderError) exception fallbacks in each job function --------


async def test_recognize_job_renders_streaming_links(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    from app.services.recognition.base import StreamingLink

    await _seed_user(50)
    await _seed_job("job-50", 50, "recognize")

    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"x"))

    async def fake_probe(path):
        from app.services.media.ffmpeg_tools import MediaProbe

        return MediaProbe(duration_seconds=10.0, has_video=False, has_audio=True, format_name="mp3", size_bytes=100)

    monkeypatch.setattr(worker_ctx.media_tools, "probe", fake_probe)
    monkeypatch.setattr(
        worker_ctx.media_tools, "extract_recognition_clip", lambda source, job_dir, clip_seconds: _write_and_return(job_dir / "clip.mp3")
    )

    result = RecognitionResult(
        matched=True,
        title="Linked Song",
        artist="Artist",
        album="Album Name",
        links=(StreamingLink(platform="spotify", url="https://open.spotify.com/track/x"),),
    )
    monkeypatch.setattr(worker_ctx.recognition_provider, "identify", AsyncMock(return_value=result))

    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-50", user_id=50, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    _, kwargs = mock_bot.edit_message_text.call_args
    assert "spotify" in kwargs["text"].lower()
    assert "Album Name" in kwargs["text"]


async def test_download_job_delivers_video_via_send_video(db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch) -> None:
    await _seed_user(51)
    await _seed_job("job-51", 51, "download", platform="youtube")

    probe = _fake_video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-51", probe)

    async def fake_download(probe_arg, format_id, job_dir, **kwargs):
        path = job_dir / "video.mp4"
        path.write_bytes(b"x" * 100)
        return DownloadedFile(path=path, media_type=MediaType.VIDEO, title="A Video", uploader=None, ext="mp4", size_bytes=100)

    monkeypatch.setattr(worker_ctx.download_manager, "download", fake_download)

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-51", user_id=51, chat_id=999, message_id=5, probe_job_id="probe-51", format_id="video:0"
    )

    mock_bot.send_video.assert_called_once()
    mock_bot.send_photo.assert_not_called()
    mock_bot.send_audio.assert_not_called()


async def test_probe_job_unexpected_error_still_fails_job_cleanly(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    """A non-DownloaderError exception (e.g. a bug elsewhere) must still be
    caught by the job's top-level boundary and recorded as failed -- never
    propagate out and crash the arq worker process (see module docstring)."""
    await _seed_user(52)
    await _seed_job("job-52", 52, "probe", platform="youtube")

    monkeypatch.setattr(worker_ctx.download_manager, "probe", AsyncMock(side_effect=RuntimeError("totally unexpected")))

    await probe_job({"worker_ctx": worker_ctx}, job_id="job-52", user_id=52, chat_id=999, message_id=5, url="https://youtube.com/watch?v=x")

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-52")
        assert job.status == "failed"
    _, kwargs = mock_bot.edit_message_text.call_args
    assert kwargs["text"] == "⚠️ Something went wrong. Please try again in a moment."


async def test_download_job_unexpected_error_still_fails_job_cleanly(db_engine, worker_ctx, mock_bot, fake_redis, monkeypatch) -> None:
    await _seed_user(53)
    await _seed_job("job-53", 53, "download", platform="youtube")

    probe = _fake_video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-53", probe)

    monkeypatch.setattr(worker_ctx.download_manager, "download", AsyncMock(side_effect=RuntimeError("totally unexpected")))

    await download_job(
        {"worker_ctx": worker_ctx}, job_id="job-53", user_id=53, chat_id=999, message_id=5, probe_job_id="probe-53", format_id="video:0"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-53")
        assert job.status == "failed"


async def test_convert_job_unexpected_error_still_fails_job_cleanly(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(54)
    await _seed_job("job-54", 54, "convert")

    mock_bot.download = AsyncMock(side_effect=RuntimeError("totally unexpected"))

    await convert_job({"worker_ctx": worker_ctx}, job_id="job-54", user_id=54, chat_id=999, message_id=5, source_file_id="file-xyz")

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-54")
        assert job.status == "failed"


async def test_fail_job_still_marks_failed_even_if_edit_message_raises(db_engine, worker_ctx, mock_bot, monkeypatch) -> None:
    await _seed_user(40)
    await _seed_job("job-40", 40, "recognize")

    mock_bot.edit_message_text = AsyncMock(side_effect=TelegramBadRequest(method=None, message="message to edit not found"))
    monkeypatch.setattr(worker_ctx.media_tools, "probe", AsyncMock(side_effect=MediaValidationError("bad file")))
    mock_bot.download = AsyncMock(side_effect=lambda file_id, destination: Path(destination).write_bytes(b"x"))

    # Must not raise even though edit_message_text itself raises.
    await recognize_job(
        {"worker_ctx": worker_ctx}, job_id="job-40", user_id=40, chat_id=999, message_id=5, source_file_id="file-abc"
    )

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        job = await JobRepository.get(session, "job-40")
        assert job.status == "failed"  # still recorded, even though notifying the user failed
