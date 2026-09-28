"""Tests for app/bot/handlers/search.py end-to-end via a real aiogram
Dispatcher (only the Bot HTTP session + arq enqueue are faked; the search
session cache is a real fakeredis round trip).
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

from app.bot.callback_data import SearchCancelCallback, SearchPageCallback, SearchSelectCallback
from app.bot.handlers.search import router as search_router
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.throttling import ThrottlingMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.bot.search_cache import SearchSession, store_search_session
from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.services.ratelimit.limiter import RateLimiter
from app.services.search.base import SearchResult


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._next_message_id = 300

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
    search_router._parent_router = None


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
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", RATE_LIMIT_RECOGNIZE_PER_MINUTE=20)


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
    dp.include_router(search_router)
    return dp


def _text_update(text: str, *, user_id: int = 1, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=update_id, message=message)


def _callback_update(data: str, *, user_id: int = 1, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice")
    message = Message(message_id=5, date=0, chat=chat, from_user=tg_user, text="results")
    callback = CallbackQuery(id="cb1", from_user=tg_user, chat_instance="x", data=data, message=message)
    return Update(update_id=update_id, callback_query=callback)


def _session(user_id: int, n: int = 23) -> SearchSession:
    results = tuple(
        SearchResult(
            title=f"Track {i}", artist="Artist", album="Al", duration_seconds=200,
            preview_url="https://p" if i == 0 else None, official_url="https://o",
            artwork_url="https://a", is_downloadable_preview=(i == 0),
        )
        for i in range(n)
    )
    return SearchSession(user_id=user_id, query="q", results=results, per_page=10)


# --- text query -> search_job -----------------------------------------------


async def test_plain_text_enqueues_search_job(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _text_update("dua lipa levitating", user_id=5))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "search_job"
    assert kwargs["query"] == "dua lipa levitating"


async def test_command_text_is_not_a_search_query(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _text_update("/start", user_id=5))
    mock_arq_pool.enqueue_job.assert_not_called()


async def test_url_text_is_not_a_search_query(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _text_update("look https://youtube.com/watch?v=x", user_id=5))
    mock_arq_pool.enqueue_job.assert_not_called()


# --- paging / cancel / select callbacks -------------------------------------


async def test_search_page_callback_edits_to_next_page(db_engine, bot, dispatcher, fake_redis) -> None:
    b, recording = bot
    await store_search_session(fake_redis, "tokP", _session(5))

    await dispatcher.feed_update(b, _callback_update(SearchPageCallback(token="tokP", page=1).pack(), user_id=5))

    edits = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"]
    assert edits and edits[-1].reply_markup is not None


async def test_search_page_callback_rejected_for_wrong_user(db_engine, bot, dispatcher, fake_redis) -> None:
    b, recording = bot
    await store_search_session(fake_redis, "tokU", _session(5))

    await dispatcher.feed_update(b, _callback_update(SearchPageCallback(token="tokU", page=1).pack(), user_id=999))

    # No edit for the wrong user; only a bare AnswerCallbackQuery.
    edits = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"]
    assert edits == []


async def test_search_page_expired_shows_alert(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update(SearchPageCallback(token="gone", page=1).pack(), user_id=5))
    answers = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert answers and answers[0].show_alert is True


async def test_search_cancel_edits_message(db_engine, bot, dispatcher, fake_redis) -> None:
    b, recording = bot
    await store_search_session(fake_redis, "tokC", _session(5))

    await dispatcher.feed_update(b, _callback_update(SearchCancelCallback(token="tokC").pack(), user_id=5))

    edits = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"]
    from app.i18n.translator import Translator

    assert edits and edits[-1].text == Translator("uz").t("search_cancelled")


async def test_search_select_enqueues_deliver_job_with_track_fields(db_engine, bot, dispatcher, fake_redis, mock_arq_pool) -> None:
    b, _ = bot
    await store_search_session(fake_redis, "tokS", _session(5))

    # index 0 has a preview_url
    await dispatcher.feed_update(b, _callback_update(SearchSelectCallback(token="tokS", index=0).pack(), user_id=5))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "search_deliver_job"
    assert kwargs["title"] == "Track 0"
    assert kwargs["preview_url"] == "https://p"
    assert kwargs["official_url"] == "https://o"


async def test_search_select_rejected_for_wrong_user(db_engine, bot, dispatcher, fake_redis, mock_arq_pool) -> None:
    b, _ = bot
    await store_search_session(fake_redis, "tokSU", _session(5))

    await dispatcher.feed_update(b, _callback_update(SearchSelectCallback(token="tokSU", index=0).pack(), user_id=999))

    mock_arq_pool.enqueue_job.assert_not_called()
