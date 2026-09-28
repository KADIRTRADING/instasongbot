"""Tests for app/bot/handlers/recognize.py, exercising the REAL aiogram
Dispatcher end-to-end (feed_update) with the real middleware stack, a real
in-memory SQLite DB, and a real fakeredis-backed arq pool substitute (we
verify the enqueue call shape directly rather than running a real worker —
the worker side is already covered by tests/test_ratelimit*.py and the
earlier live worker-integration testing).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Audio, Chat, Message, Update, Voice
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers.recognize import router as recognize_router
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

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if method.__class__.__name__ == "SendMessage":

            class _Resp:
                message_id = 555

            return _Resp()
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
    recognize_router._parent_router = None


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
    return Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        MAX_TELEGRAM_FETCH_MB=20,
        RATE_LIMIT_RECOGNIZE_PER_MINUTE=5,
    )


@pytest.fixture
async def bot():
    session = RecordingSession()
    b = Bot(token="123456:ABCDEF_fake_test_token", session=session)
    yield b, session
    await b.session.close()


@pytest.fixture
async def fake_redis():
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def mock_arq_pool() -> AsyncMock:
    return AsyncMock()


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
    dp.include_router(recognize_router)
    return dp


def _voice_update(*, user_id: int = 1, file_size: int | None = 1_000_000, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    voice = Voice(file_id="voice-abc", file_unique_id="u1", duration=5, file_size=file_size)
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, voice=voice)
    return Update(update_id=update_id, message=message)


def _audio_update(*, user_id: int = 1, file_size: int | None = 2_000_000, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    audio = Audio(file_id="audio-abc", file_unique_id="u2", duration=10, file_size=file_size)
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, audio=audio)
    return Update(update_id=update_id, message=message)


# --- Happy path: voice / audio (video uploads are handled by
# handlers/convert.py instead — see its own test file) --------------------


async def test_voice_message_enqueues_recognize_job(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _voice_update(user_id=42))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "recognize_job"
    assert kwargs["user_id"] == 42
    assert kwargs["chat_id"] == 999
    assert kwargs["source_file_id"] == "voice-abc"
    assert kwargs["message_id"] == 555  # from the progress message send response
    assert "job_id" in kwargs

    # Progress message was sent before enqueueing.
    send_calls = [c for c in recording.calls if c.__class__.__name__ == "SendMessage"]
    assert len(send_calls) == 1


async def test_audio_message_enqueues_recognize_job(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _audio_update(user_id=43))

    mock_arq_pool.enqueue_job.assert_called_once()
    _, kwargs = mock_arq_pool.enqueue_job.call_args
    assert kwargs["source_file_id"] == "audio-abc"


async def test_recognize_job_row_created_in_db(db_engine, bot, dispatcher) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _voice_update(user_id=45))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        from sqlalchemy import select

        from app.db.models import Job

        result = await session.execute(select(Job).where(Job.user_id == 45))
        job = result.scalar_one()
        assert job.job_type == JobType.RECOGNIZE.value
        assert job.status == "pending"  # not yet picked up by a worker


# --- File-too-big guard ------------------------------------------------


async def test_voice_exceeding_fetch_limit_is_rejected_without_enqueueing(
    db_engine, bot, dispatcher, mock_arq_pool
) -> None:
    b, recording = bot
    too_big = 25 * 1024 * 1024  # > MAX_TELEGRAM_FETCH_MB=20
    await dispatcher.feed_update(b, _voice_update(user_id=46, file_size=too_big))

    mock_arq_pool.enqueue_job.assert_not_called()
    send_calls = [c for c in recording.calls if c.__class__.__name__ == "SendMessage"]
    assert len(send_calls) == 1
    assert "20" in send_calls[0].text  # limit_mb mentioned in the error message


async def test_voice_with_unknown_file_size_is_allowed_through(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    """Telegram doesn't always report file_size (e.g. for some voice notes) --
    absence of a size must not block the request; only a KNOWN oversized file
    should be rejected."""
    b, _ = bot
    await dispatcher.feed_update(b, _voice_update(user_id=47, file_size=None))

    mock_arq_pool.enqueue_job.assert_called_once()


# --- Rate limiting integration -----------------------------------------


async def test_recognize_rate_limit_denies_after_configured_count(db_engine, bot, dispatcher, mock_arq_pool, settings) -> None:
    b, recording = bot
    settings.RATE_LIMIT_RECOGNIZE_PER_MINUTE = 1

    await dispatcher.feed_update(b, _voice_update(user_id=48, update_id=1))
    await dispatcher.feed_update(b, _voice_update(user_id=48, update_id=2))

    assert mock_arq_pool.enqueue_job.call_count == 1  # second was throttled

