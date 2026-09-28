"""Tests for app/bot/handlers/convert.py (uploaded-video auto-recognition),
exercising the REAL aiogram Dispatcher end-to-end with real middleware, real
SQLite, and a real fakeredis-backed result cache round trip.

In the automatic UX a video upload is auto-recognized (no "what would you like
to do?" prompt): the handler stashes the file_id in the result cache and
enqueues a recognize_job carrying a result_token, and the worker attaches an
"Extract MP3" button to the result. The Extract-MP3 / Find-song callbacks
themselves are owned by handlers/download.py's single ResultAction handler
(tested in test_download_handler.py), so this file only covers the upload
entry point.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update, Video
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers.convert import router as convert_router
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.config import Settings
from app.constants import JobType
from app.db import session as db_session_module
from app.db.base import Base
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
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", RATE_LIMIT_RECOGNIZE_PER_MINUTE=5)


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


def _video_upload_update(*, user_id: int = 1, update_id: int = 1, file_size: int | None = None) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    video = Video(file_id="video-xyz", file_unique_id="u1", duration=15, width=10, height=10, file_size=file_size)
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, video=video)
    return Update(update_id=update_id, message=message)


# --- Video upload -> automatic recognition -----------------------------------


async def test_video_upload_auto_enqueues_recognize_job_with_result_token(
    db_engine, bot, dispatcher, mock_arq_pool, fake_redis
) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _video_upload_update(user_id=1))

    # It shows a progress message and immediately enqueues recognition — no
    # intermediate "choose an action" keyboard.
    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "recognize_job"
    assert kwargs["source_file_id"] == "video-xyz"
    token = kwargs["result_token"]
    assert token

    # The progress message carries no inline keyboard (recognition runs first).
    assert len(recording.calls) == 1
    assert getattr(recording.calls[0], "reply_markup", None) is None


async def test_video_upload_stashes_file_id_in_result_cache(db_engine, bot, dispatcher, mock_arq_pool, fake_redis) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _video_upload_update(user_id=3))

    _, kwargs = mock_arq_pool.enqueue_job.call_args
    token = kwargs["result_token"]

    from app.bot.result_cache import load_result_context

    ctx = await load_result_context(fake_redis, token)
    assert ctx is not None
    assert ctx.file_id == "video-xyz"
    assert ctx.platform == "upload"
    assert ctx.belongs_to(3)


async def test_video_upload_creates_recognize_job_row(db_engine, bot, dispatcher) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _video_upload_update(user_id=12))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        from app.db.models import Job

        job = (await session.execute(select(Job).where(Job.user_id == 12))).scalar_one()
        assert job.job_type == JobType.RECOGNIZE.value


async def test_video_upload_too_large_rejected_without_enqueue(db_engine, bot, dispatcher, mock_arq_pool, settings) -> None:
    b, recording = bot
    too_big = (settings.MAX_TELEGRAM_FETCH_MB + 5) * 1024 * 1024
    await dispatcher.feed_update(b, _video_upload_update(user_id=1, file_size=too_big))

    mock_arq_pool.enqueue_job.assert_not_called()
    assert len(recording.calls) == 1
    assert "❌" in recording.calls[0].text
