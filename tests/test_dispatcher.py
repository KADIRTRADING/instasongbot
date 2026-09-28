"""Tests for app/bot/dispatcher.py's `build_dispatcher()` factory.

These are true integration/smoke tests: a real Dispatcher, built exactly the
way production does, fed real Updates end-to-end (feed_update), with only
the Bot's HTTP session faked (RecordingSession, same pattern as every other
handler test file) and Redis faked via fakeredis. The goal is to prove the
WIRING is correct — router order, middleware order, and the interaction
between them — not to re-test individual handler behavior already covered by
tests/test_admin_handlers.py, test_core_handlers.py, etc.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.base import StorageKey
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.dispatcher import build_bot, build_dispatcher
from app.bot.handlers import admin, convert, core, download, recognize
from app.bot.states import AdminButtonStates
from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import CaptionRepository
from app.services.ratelimit.limiter import RateLimiter

ADMIN_ID = 777
NON_ADMIN_ID = 1


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._next_message_id = 100

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
def reset_router_parents():
    # build_dispatcher() includes every module-level router singleton into a
    # fresh Dispatcher each call; aiogram enforces one-parent-per-router, so
    # every test needs a clean slate (see other handler test files for the
    # same pattern, applied there to one router at a time).
    yield
    for module in (admin, core, recognize, convert, download):
        module.router._parent_router = None


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
        ADMIN_IDS=str(ADMIN_ID),
        RATE_LIMIT_DOWNLOAD_PER_MINUTE=1,
    )


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
def mock_arq_pool() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def dispatcher(db_engine, settings: Settings, fake_redis, mock_arq_pool: AsyncMock):
    limiter = RateLimiter(fake_redis)
    return build_dispatcher(settings, arq_pool=mock_arq_pool, rate_limiter=limiter)


def _message_update(text: str, *, user_id: int, update_id: int, chat_id: int = 999) -> Update:
    chat = Chat(id=chat_id, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="T")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=update_id, message=message)


# --- Basic wiring: workflow_data, error handler registered -----------------


def test_build_dispatcher_injects_settings_into_workflow_data(dispatcher, settings: Settings) -> None:
    assert dispatcher["settings"] is settings


def test_build_dispatcher_registers_error_handler(dispatcher) -> None:
    from app.bot.error_handler import handle_unexpected_error

    registered_callbacks = [h.callback for h in dispatcher.errors.handlers]
    assert handle_unexpected_error in registered_callbacks


def test_build_bot_uses_html_parse_mode(settings: Settings) -> None:
    b = build_bot(settings)
    assert b.default.parse_mode == "HTML"


# --- Router order: admin must win over content-sniffing routers ------------


async def test_non_admin_start_command_reaches_core_router(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/start", user_id=NON_ADMIN_ID, update_id=1))

    assert len(recording.calls) == 1
    assert recording.calls[0].__class__.__name__ == "SendMessage"


async def test_non_admin_cannot_reach_admin_menu(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/admin", user_id=NON_ADMIN_ID, update_id=1))

    assert recording.calls == []  # admin router's is_admin gate blocked it


async def test_admin_reaches_admin_menu(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/admin", user_id=ADMIN_ID, update_id=1))

    assert len(recording.calls) == 1


async def test_admin_mid_flow_url_not_hijacked_by_download_router(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    """The core regression this dispatcher wiring exists to prevent: an admin
    supplying a supported-platform URL as FSM input (e.g. a caption button's
    destination) must be captured by admin_router, not stolen by
    download_router's un-gated "any text with a known link" matcher. See
    app/bot/dispatcher.py's `_include_routers` docstring for the full story.
    """
    b, _ = bot
    key = StorageKey(bot_id=b.id, chat_id=999, user_id=ADMIN_ID)
    await dispatcher.storage.set_state(key=key, state=AdminButtonStates.waiting_for_url)
    await dispatcher.storage.set_data(key=key, data={"label": "Channel", "media_type": None})

    await dispatcher.feed_update(
        b, _message_update("https://www.youtube.com/@somechannel", user_id=ADMIN_ID, update_id=1)
    )

    mock_arq_pool.enqueue_job.assert_not_called()  # no probe_job was triggered

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        buttons = await CaptionRepository.list_all_buttons(session)
    assert len(buttons) == 1
    assert buttons[0].url == "https://www.youtube.com/@somechannel"


async def test_regular_user_url_still_reaches_download_router(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    """Confirms admin_router's is_admin gate doesn't accidentally swallow
    updates for non-admins -- the fix for the routing-order bug must not
    itself break the ordinary download flow.
    """
    b, _ = bot
    await dispatcher.feed_update(
        b, _message_update("https://www.youtube.com/watch?v=abc123", user_id=NON_ADMIN_ID, update_id=1)
    )

    mock_arq_pool.enqueue_job.assert_called_once()
    args, _kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "probe_job"


# --- Middleware wiring: throttling is genuinely active as an inner mw ------


async def test_throttling_denies_second_download_within_window(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    await dispatcher.feed_update(
        b, _message_update("https://www.youtube.com/watch?v=abc123", user_id=NON_ADMIN_ID, update_id=1)
    )
    mock_arq_pool.enqueue_job.assert_called_once()

    mock_arq_pool.enqueue_job.reset_mock()
    await dispatcher.feed_update(
        b, _message_update("https://www.tiktok.com/@x/video/123", user_id=NON_ADMIN_ID, update_id=2)
    )

    mock_arq_pool.enqueue_job.assert_not_called()  # denied by RATE_LIMIT_DOWNLOAD_PER_MINUTE=1
    deny_calls = [c for c in recording.calls if c.__class__.__name__ == "SendMessage"]
    assert any("⏱" in (c.text or "") for c in deny_calls)


async def test_throttling_independent_per_user(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, _ = bot
    await dispatcher.feed_update(
        b, _message_update("https://www.youtube.com/watch?v=abc123", user_id=10, update_id=1)
    )
    await dispatcher.feed_update(
        b, _message_update("https://www.youtube.com/watch?v=def456", user_id=11, update_id=2)
    )

    assert mock_arq_pool.enqueue_job.call_count == 2  # different users, independent limits


# --- Banned users are blocked before reaching any router -------------------


async def test_banned_user_blocked_before_any_handler(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    from app.db.repositories import UserRepository

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await UserRepository.get_or_create(session, user_id=50, username=None, first_name=None, default_language="en")
        await UserRepository.set_banned(session, 50, True)
        await session.commit()

    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/start", user_id=50, update_id=1))

    mock_arq_pool.enqueue_job.assert_not_called()
    assert len(recording.calls) == 1  # only the "you're banned" notice
    assert "🚫" in recording.calls[0].text
