"""Tests for app/bot/handlers/download.py, exercising the REAL aiogram
Dispatcher end-to-end with real middleware, real SQLite, and a real
fakeredis-backed probe cache (so load_probe_result/store_probe_result round
trips genuinely happen, not mocked) — only the arq enqueue call itself and
the Bot's HTTP session are faked/recorded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.callback_data import DownloadFormatCallback
from app.bot.handlers.download import router as download_router
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.bot.probe_cache import store_probe_result
from app.config import Settings
from app.constants import JobType, MediaType
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import PlatformRepository
from app.services.downloader.models import MediaFormat, ProbeResult
from app.services.ratelimit.limiter import RateLimiter


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._next_message_id = 100

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        name = method.__class__.__name__
        if name == "SendMessage":
            self._next_message_id += 1

            class _Msg:
                message_id = self._next_message_id
                date = 0

            return _Msg()
        if name == "EditMessageText":

            class _Msg2:
                message_id = getattr(method, "message_id", 1)
                date = 0

            return _Msg2()
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
    download_router._parent_router = None


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
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", RATE_LIMIT_DOWNLOAD_PER_MINUTE=5)


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
    """A mock whose enqueue_job is inspectable, but which otherwise proxies
    Redis calls (get/set, used by probe_cache) to a real fakeredis instance —
    ArqRedis IS a redis.asyncio.Redis subclass in production, so the handler
    code calls .get()/.set() on the same object it calls .enqueue_job() on.
    """
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
    dp.include_router(download_router)
    return dp


def _text_update(text: str, *, user_id: int = 1, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=update_id, message=message)


def _callback_update(data: str, *, user_id: int = 1, update_id: int = 1, message_id: int = 5) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    message = Message(message_id=message_id, date=0, chat=chat, from_user=tg_user, text="📥 Choose what you'd like to download:")
    callback = CallbackQuery(id="cb1", from_user=tg_user, chat_instance="x", data=data, message=message)
    return Update(update_id=update_id, callback_query=callback)


def _sample_probe(platform: str = "pinterest", n_images: int = 1) -> ProbeResult:
    formats = tuple(
        MediaFormat(format_id=f"carousel:{i}" if n_images > 1 else "image:0", media_type=MediaType.IMAGE, label=f"Image {i + 1}", ext="jpg")
        for i in range(n_images)
    )
    return ProbeResult(
        platform=platform,
        source_url="https://www.pinterest.com/pin/123/",
        title="Test",
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=formats,
    )


# --- Link detection --------------------------------------------------


async def test_message_with_pinterest_link_enqueues_auto_download_job(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _text_update("check this out https://www.pinterest.com/pin/123456789/ nice"))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "auto_download_job"
    assert kwargs["url"] == "https://www.pinterest.com/pin/123456789/"
    assert kwargs["chat_id"] == 999


async def test_message_with_no_link_does_not_trigger_download_handler(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    # With only the download router registered, plain text (no URL) matches
    # nothing here (it would go to the search router in the full app).
    b, recording = bot
    await dispatcher.feed_update(b, _text_update("just some regular text, no links"))

    mock_arq_pool.enqueue_job.assert_not_called()
    assert len(recording.calls) == 0


async def test_message_with_unsupported_link_does_not_trigger_handler(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _text_update("check out https://vimeo.com/12345"))

    mock_arq_pool.enqueue_job.assert_not_called()


async def test_instagram_share_url_is_normalized_before_enqueue(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, _ = bot
    await dispatcher.feed_update(
        b, _text_update("https://www.instagram.com/reels/ABC123/?igsh=trackingnoise", user_id=42)
    )

    mock_arq_pool.enqueue_job.assert_called_once()
    _, kwargs = mock_arq_pool.enqueue_job.call_args
    # /reels/ collapsed to /reel/ and the igsh tracking param stripped.
    assert kwargs["url"] == "https://www.instagram.com/reel/ABC123/"


async def test_auto_download_job_row_created_with_correct_platform(db_engine, bot, dispatcher) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _text_update("https://www.tiktok.com/@user/video/123", user_id=42))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        from app.db.models import Job

        job = (await session.execute(select(Job).where(Job.user_id == 42))).scalar_one()
        assert job.job_type == JobType.DOWNLOAD.value
        assert job.platform == "tiktok"


async def test_disabled_platform_rejects_without_enqueueing(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await PlatformRepository.set_enabled(session, "tiktok", False)
        await session.commit()

    await dispatcher.feed_update(b, _text_update("https://www.tiktok.com/@user/video/123"))

    mock_arq_pool.enqueue_job.assert_not_called()
    assert len(recording.calls) == 1
    assert "disabled" in recording.calls[0].text.lower() or "❌" in recording.calls[0].text


# --- Result-action callback (Find song / Extract MP3 / Other) ----------


async def test_result_action_find_reuses_file_id_via_recognize_job(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    from app.bot.callback_data import ResultActionCallback
    from app.bot.result_cache import ResultActionContext, store_result_context

    b, recording = bot
    await store_result_context(
        fake_redis,
        "rtok1",
        ResultActionContext(user_id=7, file_id="FILEID99", source_url="https://x", platform="tiktok", probe_job_id="pj"),
    )

    data = ResultActionCallback(token="rtok1", action="find").pack()
    await dispatcher.feed_update(b, _callback_update(data, user_id=7))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "recognize_job"
    assert kwargs["source_file_id"] == "FILEID99"


async def test_result_action_mp3_reuses_file_id_via_convert_job(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    from app.bot.callback_data import ResultActionCallback
    from app.bot.result_cache import ResultActionContext, store_result_context

    b, _ = bot
    await store_result_context(
        fake_redis,
        "rtok2",
        ResultActionContext(user_id=7, file_id="FILEID77", source_url="https://x", platform="tiktok", probe_job_id="pj"),
    )

    data = ResultActionCallback(token="rtok2", action="mp3").pack()
    await dispatcher.feed_update(b, _callback_update(data, user_id=7))

    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "convert_job"
    assert kwargs["source_file_id"] == "FILEID77"


async def test_result_action_other_shows_format_keyboard(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    from app.bot.callback_data import ResultActionCallback
    from app.bot.result_cache import ResultActionContext, store_result_context

    b, recording = bot
    probe = _sample_probe(n_images=1)
    await store_probe_result(fake_redis, "pjOther", probe)
    await store_result_context(
        fake_redis,
        "rtok3",
        ResultActionContext(user_id=7, file_id="F", source_url="https://x", platform="pinterest", probe_job_id="pjOther"),
    )

    data = ResultActionCallback(token="rtok3", action="other").pack()
    await dispatcher.feed_update(b, _callback_update(data, user_id=7))

    mock_arq_pool.enqueue_job.assert_not_called()
    # A message with a format keyboard is sent.
    send_calls = [c for c in recording.calls if c.__class__.__name__ == "SendMessage"]
    assert send_calls and send_calls[-1].reply_markup is not None


async def test_result_action_rejected_for_wrong_user(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    from app.bot.callback_data import ResultActionCallback
    from app.bot.result_cache import ResultActionContext, store_result_context

    b, _ = bot
    await store_result_context(
        fake_redis,
        "rtok4",
        ResultActionContext(user_id=7, file_id="F", source_url="https://x", platform="tiktok", probe_job_id="pj"),
    )

    data = ResultActionCallback(token="rtok4", action="find").pack()
    await dispatcher.feed_update(b, _callback_update(data, user_id=999))  # different user

    mock_arq_pool.enqueue_job.assert_not_called()


async def test_result_action_expired_context_shows_alert(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    from app.bot.callback_data import ResultActionCallback

    b, recording = bot
    data = ResultActionCallback(token="never", action="find").pack()
    await dispatcher.feed_update(b, _callback_update(data, user_id=7))

    mock_arq_pool.enqueue_job.assert_not_called()
    answer_calls = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert answer_calls and answer_calls[0].show_alert is True


# --- Format-selection callback -----------------------------------------


async def test_format_selection_enqueues_download_job(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    b, recording = bot
    probe = _sample_probe(n_images=1)
    await store_probe_result(fake_redis, "probe-job-1", probe)

    callback_data = DownloadFormatCallback(probe_job_id="probe-job-1", format_index=0).pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=7))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "download_job"
    assert kwargs["probe_job_id"] == "probe-job-1"
    assert kwargs["format_id"] == "image:0"
    assert kwargs["user_id"] == 7

    method_names = [c.__class__.__name__ for c in recording.calls]
    assert "AnswerCallbackQuery" in method_names
    assert "EditMessageText" in method_names


async def test_format_selection_download_all_enqueues_one_job_per_image(
    db_engine, bot, dispatcher, mock_arq_pool, fake_redis
) -> None:
    b, recording = bot
    probe = _sample_probe(n_images=3)
    await store_probe_result(fake_redis, "probe-job-carousel", probe)

    callback_data = DownloadFormatCallback(probe_job_id="probe-job-carousel", format_index="all").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=8))

    assert mock_arq_pool.enqueue_job.call_count == 3
    format_ids = {call.kwargs["format_id"] for call in mock_arq_pool.enqueue_job.call_args_list}
    assert format_ids == {"carousel:0", "carousel:1", "carousel:2"}

    # Each job must have gotten a DIFFERENT message_id (own progress message)
    # -- otherwise concurrent delivery/failure of one job could corrupt the
    # others' ability to edit/delete their status message.
    message_ids = [call.kwargs["message_id"] for call in mock_arq_pool.enqueue_job.call_args_list]
    assert len(set(message_ids)) == 3


async def test_format_selection_creates_one_job_row_per_download(db_engine, bot, dispatcher, fake_redis) -> None:
    b, _ = bot
    probe = _sample_probe(n_images=2)
    await store_probe_result(fake_redis, "probe-job-2", probe)

    callback_data = DownloadFormatCallback(probe_job_id="probe-job-2", format_index="all").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=9))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        from app.db.models import Job

        jobs = (await session.execute(select(Job).where(Job.user_id == 9))).scalars().all()
        assert len(jobs) == 2
        assert all(j.job_type == JobType.DOWNLOAD.value for j in jobs)


async def test_format_selection_with_expired_probe_shows_error(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    # No store_probe_result call -- simulates a TTL-expired or never-existed probe.
    callback_data = DownloadFormatCallback(probe_job_id="nonexistent-job", format_index=0).pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=10))

    mock_arq_pool.enqueue_job.assert_not_called()
    answer_calls = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert len(answer_calls) == 1
    assert answer_calls[0].show_alert is True


async def test_format_selection_out_of_range_index_shows_error(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    b, recording = bot
    probe = _sample_probe(n_images=1)
    await store_probe_result(fake_redis, "probe-job-3", probe)

    callback_data = DownloadFormatCallback(probe_job_id="probe-job-3", format_index=99).pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=11))

    mock_arq_pool.enqueue_job.assert_not_called()
