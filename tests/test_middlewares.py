"""Tests for the bot middlewares, exercising the REAL aiogram Dispatcher
dispatch chain (feed_update) against a real in-memory SQLite DB and a real
fakeredis+lupa Redis, with only the Bot's HTTP session faked out.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from aiogram import Bot, Dispatcher, Router, flags
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import create_async_engine

from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import UserRepository
from app.services.ratelimit.limiter import RateLimiter


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        # Return a minimal plausible value for whatever was requested so
        # aiogram's response-type validation (e.g. AnswerCallbackQuery -> bool)
        # doesn't itself raise.
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture(autouse=True)
async def reset_db_engine():
    # init_engine()/get_sessionmaker() are process-global singletons (see
    # app/db/session.py) -- reset them between tests so each test gets its
    # own fresh in-memory SQLite engine instead of leaking state.
    await db_session_module.dispose_engine()
    yield
    await db_session_module.dispose_engine()


@pytest.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    db_session_module._engine = engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    db_session_module._sessionmaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield engine
    await engine.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", ADMIN_IDS="777")


@pytest.fixture
async def bot():
    session = RecordingSession()
    b = Bot(token="123456:ABCDEF_fake_test_token", session=session)
    yield b, session
    await b.session.close()


def _build_message_update(user_id: int = 42, text: str = "hi", update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Test", username="tester")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=update_id, message=message)


# --- DbSessionMiddleware --------------------------------------------------


async def test_db_session_middleware_injects_session_and_commits(db_engine, bot, settings) -> None:
    b, recording = bot
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())

    received_session = {}

    router = Router()

    @router.message()
    async def handler(message: Message, session) -> None:
        received_session["session"] = session
        await UserRepository.get_or_create(
            session, user_id=555, username="x", first_name="X", default_language="uz"
        )

    dp.include_router(router)
    await dp.feed_update(b, _build_message_update())

    assert "session" in received_session

    # Commit happened -- a fresh session should see the row.
    sessionmaker = db_session_module.get_sessionmaker()
    async with sessionmaker() as verify_session:
        user = await UserRepository.get(verify_session, 555)
        assert user is not None


async def test_db_session_middleware_rolls_back_on_exception(db_engine, bot, settings) -> None:
    b, _ = bot
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())

    router = Router()

    @router.message()
    async def handler(message: Message, session) -> None:
        await UserRepository.get_or_create(
            session, user_id=556, username="x", first_name="X", default_language="uz"
        )
        raise RuntimeError("boom")

    dp.include_router(router)
    dp.errors.register(lambda event, **kw: None)  # swallow so feed_update doesn't propagate
    await dp.feed_update(b, _build_message_update())

    sessionmaker = db_session_module.get_sessionmaker()
    async with sessionmaker() as verify_session:
        user = await UserRepository.get(verify_session, 556)
        assert user is None  # rolled back, never committed


# --- UserContextMiddleware -------------------------------------------------


async def test_user_context_middleware_creates_user_and_injects_translator(db_engine, bot, settings) -> None:
    b, _ = bot
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))

    captured = {}

    router = Router()

    @router.message()
    async def handler(message: Message, db_user, translator, is_admin: bool) -> None:
        captured["db_user"] = db_user
        captured["translator"] = translator
        captured["is_admin"] = is_admin

    dp.include_router(router)
    await dp.feed_update(b, _build_message_update(user_id=42))

    assert captured["db_user"].id == 42
    assert captured["translator"].language == "uz"  # DEFAULT_LANGUAGE
    assert captured["is_admin"] is False


async def test_user_context_middleware_recognizes_admin(db_engine, bot, settings) -> None:
    b, _ = bot
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))

    captured = {}
    router = Router()

    @router.message()
    async def handler(message: Message, is_admin: bool) -> None:
        captured["is_admin"] = is_admin

    dp.include_router(router)
    await dp.feed_update(b, _build_message_update(user_id=777))  # matches settings.ADMIN_IDS

    assert captured["is_admin"] is True


async def test_user_context_middleware_blocks_banned_user(db_engine, bot, settings) -> None:
    b, recording = bot
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))

    handler_called = {"value": False}
    router = Router()

    @router.message()
    async def handler(message: Message) -> None:
        handler_called["value"] = True

    dp.include_router(router)

    # Pre-ban the user before the update arrives, using the already-
    # initialized global engine (see the db_engine fixture).
    sm = db_session_module.get_sessionmaker()
    async with sm() as s:
        await UserRepository.get_or_create(s, user_id=99, username=None, first_name=None, default_language="uz")
        await UserRepository.set_banned(s, 99, True)
        await s.commit()

    await dp.feed_update(b, _build_message_update(user_id=99))

    assert handler_called["value"] is False
    assert len(recording.calls) == 1
    assert recording.calls[0].__class__.__name__ == "SendMessage"


# --- ThrottlingMiddleware ---------------------------------------------------


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


async def test_throttling_middleware_allows_within_burst_limit(db_engine, bot, settings, fake_redis) -> None:
    b, _ = bot
    limiter = RateLimiter(fake_redis)
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    dp.message.middleware(ThrottlingMiddleware(limiter, settings))  # INNER, not outer -- see throttling.py docstring

    call_count = {"value": 0}
    router = Router()

    @router.message()
    async def handler(message: Message) -> None:
        call_count["value"] += 1

    dp.include_router(router)
    await dp.feed_update(b, _build_message_update(user_id=1, update_id=1))

    assert call_count["value"] == 1


async def test_throttling_middleware_denies_action_over_per_action_limit(db_engine, bot, settings, fake_redis) -> None:
    b, recording = bot
    limiter = RateLimiter(fake_redis)
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    dp.message.middleware(ThrottlingMiddleware(limiter, settings))  # INNER, not outer -- see throttling.py docstring

    # Force the per-action limit down to 1 via env override on settings.
    settings.RATE_LIMIT_DOWNLOAD_PER_MINUTE = 1

    call_count = {"value": 0}
    router = Router()

    @router.message()
    @flags.rate_limit("download")
    async def handler(message: Message) -> None:
        call_count["value"] += 1

    dp.include_router(router)

    await dp.feed_update(b, _build_message_update(user_id=2, update_id=1))
    await dp.feed_update(b, _build_message_update(user_id=2, update_id=2))

    assert call_count["value"] == 1  # second call denied
    # One SendMessage from the deny path.
    assert any(c.__class__.__name__ == "SendMessage" for c in recording.calls)


async def test_throttling_middleware_different_users_independent(db_engine, bot, settings, fake_redis) -> None:
    b, _ = bot
    limiter = RateLimiter(fake_redis)
    settings.RATE_LIMIT_DOWNLOAD_PER_MINUTE = 1
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    dp.message.middleware(ThrottlingMiddleware(limiter, settings))  # INNER, not outer -- see throttling.py docstring

    call_count = {"value": 0}
    router = Router()

    @router.message()
    @flags.rate_limit("download")
    async def handler(message: Message) -> None:
        call_count["value"] += 1

    dp.include_router(router)
    await dp.feed_update(b, _build_message_update(user_id=10, update_id=1))
    await dp.feed_update(b, _build_message_update(user_id=11, update_id=2))

    assert call_count["value"] == 2  # different users, independent limits
