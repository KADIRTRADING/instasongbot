"""Tests for app/bot/handlers/convert.py, exercising the REAL aiogram
Dispatcher end-to-end with real middleware, real SQLite, and a real
fakeredis-backed upload/probe cache round trip.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, Video
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.callback_data import VideoActionCallback
from app.bot.handlers.convert import router as convert_router
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.bot.probe_cache import store_probe_result
from app.bot.upload_cache import store_upload_file_id
from app.config import Settings
from app.constants import JobType, MediaType
from app.db import session as db_session_module
from app.db.base import Base
from app.services.downloader.models import MediaFormat, ProbeResult
from app.services.ratelimit.limiter import RateLimiter


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._next_message_id = 200

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if method.__class__.__name__ == "SendMessage":
            self._next_message_id += 1

            class _Msg:
                message_id = self._next_message_id
                date = 0

            return _Msg()
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
async def reset_db_engine():
    await db_session_module.dispose_engine()
    yield
    await db_session_module.dispose_engine()


@pytest.fixture(autouse=True)
def reset_router_parent():
    yield
    convert_router._parent_router = None


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_session_module._engine = engine
    db_session_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield engine
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", RATE_LIMIT_CONVERT_PER_MINUTE=5)


@pytest.fixture
async def bot():
    session = RecordingSession()
    b = Bot(token="123456:ABCDEF_fake_test_token", session=session)
    yield b, session
    await b.session.close()


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def mock_arq_pool(fake_redis) -> AsyncMock:
    pool = AsyncMock(wraps=fake_redis)
    pool.get = fake_redis.get
    pool.set = fake_redis.set
    pool.enqueue_job = AsyncMock()
    return pool


@pytest.fixture
def dispatcher(settings: Settings, fake_redis, mock_arq_pool: AsyncMock) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp["arq_pool"] = mock_arq_pool
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    limiter = RateLimiter(fake_redis)
    throttler = ThrottlingMiddleware(limiter, settings)
    dp.message.middleware(throttler)
    dp.callback_query.middleware(throttler)
    dp.include_router(convert_router)
    return dp


def _video_upload_update(*, user_id: int = 1, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    video = Video(file_id="video-xyz", file_unique_id="u1", duration=15, width=10, height=10)
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, video=video)
    return Update(update_id=update_id, message=message)


def _callback_update(data: str, *, user_id: int = 1, update_id: int = 1, message_id: int = 5) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    message = Message(message_id=message_id, date=0, chat=chat, from_user=tg_user, text="🎬 What would you like to do with this video?")
    callback = CallbackQuery(id="cb1", from_user=tg_user, chat_instance="x", data=data, message=message)
    return Update(update_id=update_id, callback_query=callback)


def _video_probe(n_formats: int = 1) -> ProbeResult:
    formats = tuple(
        MediaFormat(format_id=f"video:{i}", media_type=MediaType.VIDEO, label=f"{720 - i * 100}p", ext="mp4")
        for i in range(n_formats)
    )
    return ProbeResult(
        platform="youtube",
        source_url="https://www.youtube.com/watch?v=abc123",
        title="Test Video",
        uploader=None,
        thumbnail_url=None,
        duration_seconds=60.0,
        formats=formats,
    )


# --- Video upload -> 2-choice keyboard -----------------------------------


async def test_video_upload_sends_two_choice_keyboard(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _video_upload_update(user_id=1))

    mock_arq_pool.enqueue_job.assert_not_called()  # no job yet -- just showing choices
    assert len(recording.calls) == 1
    sent = recording.calls[0]
    assert len(sent.reply_markup.inline_keyboard) == 2  # identify + audio, no "download original"

    actions = [VideoActionCallback.unpack(row[0].callback_data).action for row in sent.reply_markup.inline_keyboard]
    assert actions == ["identify", "audio"]
    for row in sent.reply_markup.inline_keyboard:
        assert VideoActionCallback.unpack(row[0].callback_data).source == "upload"


async def test_video_upload_stashes_file_id_in_redis(db_engine, bot, dispatcher, fake_redis) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _video_upload_update(user_id=1))

    sent = recording.calls[0]
    ref_id = VideoActionCallback.unpack(sent.reply_markup.inline_keyboard[0][0].callback_data).ref_id

    from app.bot.upload_cache import load_upload_file_id

    stored_file_id = await load_upload_file_id(fake_redis, ref_id)
    assert stored_file_id == "video-xyz"


# --- Upload -> "identify" -------------------------------------------------


async def test_upload_identify_action_enqueues_recognize_job_with_file_id(
    db_engine, bot, dispatcher, mock_arq_pool, fake_redis
) -> None:
    b, recording = bot
    await store_upload_file_id(fake_redis, "upload-1", "video-file-abc")

    callback_data = VideoActionCallback(ref_id="upload-1", action="identify", source="upload").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=5))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "recognize_job"
    assert kwargs["source_file_id"] == "video-file-abc"
    assert kwargs.get("probe_job_id") is None


async def test_upload_audio_action_enqueues_convert_job_with_file_id(
    db_engine, bot, dispatcher, mock_arq_pool, fake_redis
) -> None:
    b, _ = bot
    await store_upload_file_id(fake_redis, "upload-2", "video-file-def")

    callback_data = VideoActionCallback(ref_id="upload-2", action="audio", source="upload").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=6))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "convert_job"
    assert kwargs["source_file_id"] == "video-file-def"


async def test_upload_action_with_expired_cache_shows_error(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    callback_data = VideoActionCallback(ref_id="never-existed", action="identify", source="upload").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=7))

    mock_arq_pool.enqueue_job.assert_not_called()
    answer_calls = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert len(answer_calls) == 1
    assert answer_calls[0].show_alert is True


# --- Link -> "identify" / "audio" / "video" ------------------------------


async def test_link_identify_action_enqueues_recognize_job_with_probe_ref(
    db_engine, bot, dispatcher, mock_arq_pool, fake_redis
) -> None:
    b, _ = bot
    probe = _video_probe(n_formats=2)
    await store_probe_result(fake_redis, "probe-1", probe)

    callback_data = VideoActionCallback(ref_id="probe-1", action="identify", source="link").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=8))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "recognize_job"
    assert kwargs["probe_job_id"] == "probe-1"
    assert kwargs["format_id"] == "video:0"
    assert kwargs.get("source_file_id") is None


async def test_link_audio_action_enqueues_convert_job_with_probe_ref(
    db_engine, bot, dispatcher, mock_arq_pool, fake_redis
) -> None:
    b, _ = bot
    probe = _video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-2", probe)

    callback_data = VideoActionCallback(ref_id="probe-2", action="audio", source="link").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=9))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "convert_job"
    assert kwargs["probe_job_id"] == "probe-2"


async def test_link_video_action_enqueues_download_job(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    b, _ = bot
    probe = _video_probe(n_formats=1)
    await store_probe_result(fake_redis, "probe-3", probe)

    callback_data = VideoActionCallback(ref_id="probe-3", action="video", source="link").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=10))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "download_job"
    assert kwargs["probe_job_id"] == "probe-3"


async def test_link_action_with_no_video_formats_shows_error(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    """A probe that somehow has no video formats (shouldn't normally reach
    this handler, but defend against it anyway) must not crash or enqueue."""
    b, recording = bot
    probe = ProbeResult(
        platform="pinterest",
        source_url="https://pinterest.com/pin/1",
        title=None,
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=(MediaFormat(format_id="image:0", media_type=MediaType.IMAGE, label="Image", ext="jpg"),),
    )
    await store_probe_result(fake_redis, "probe-no-video", probe)

    callback_data = VideoActionCallback(ref_id="probe-no-video", action="identify", source="link").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=11))

    mock_arq_pool.enqueue_job.assert_not_called()
    answer_calls = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert answer_calls[0].show_alert is True


# --- Job row bookkeeping --------------------------------------------------


async def test_video_action_creates_job_row_with_correct_type(db_engine, bot, dispatcher, fake_redis) -> None:
    b, _ = bot
    await store_upload_file_id(fake_redis, "upload-3", "video-file-ghi")

    callback_data = VideoActionCallback(ref_id="upload-3", action="audio", source="upload").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=12))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        from app.db.models import Job

        job = (await session.execute(select(Job).where(Job.user_id == 12))).scalar_one()
        assert job.job_type == JobType.CONVERT.value


# --- Menu prompt ---------------------------------------------------------


async def test_convert_menu_button_sends_prompt(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    from app.i18n.translator import Translator

    b, recording = bot
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=13, is_bot=False, first_name="Alice")
    label = Translator("en").t("menu_convert_audio")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=label)
    await dispatcher.feed_update(b, Update(update_id=1, message=message))

    mock_arq_pool.enqueue_job.assert_not_called()
    assert len(recording.calls) == 1
