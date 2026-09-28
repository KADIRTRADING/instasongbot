"""Tests for app/bot/handlers/core.py (/start, /help, /language, language
selection callback), exercising the REAL aiogram Dispatcher end-to-end
(feed_update) with the real DbSessionMiddleware + UserContextMiddleware
stack, a real in-memory SQLite DB, and only the Bot's HTTP session faked.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.callback_data import LanguageCallback
from app.bot.handlers.core import router as core_router
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import UserRepository
from app.i18n.translator import Translator


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
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
    """`core_router` is a module-level singleton (by design — production code
    includes it into exactly one Dispatcher tree at startup, see the
    dispatcher factory). aiogram enforces one-parent-per-router as a genuine
    safety rail against double-registration bugs, which means re-using the
    same Router object across multiple per-test Dispatcher instances needs
    an explicit reset between tests -- this is test-harness plumbing only,
    never done in production code.
    """
    yield
    core_router._parent_router = None


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
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", ADMIN_IDS="777")


@pytest.fixture
async def bot():
    session = RecordingSession()
    b = Bot(token="123456:ABCDEF_fake_test_token", session=session)
    yield b, session
    await b.session.close()


@pytest.fixture
def dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    dp.include_router(core_router)
    return dp


def _message_update(text: str, *, user_id: int = 42, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice", username="alice")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=update_id, message=message)


def _callback_update(data: str, *, user_id: int = 42, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Alice", username="alice")
    message = Message(message_id=5, date=0, chat=chat, from_user=tg_user, text="🌐 Choose your language:")
    callback = CallbackQuery(id="cb1", from_user=tg_user, chat_instance="x", data=data, message=message)
    return Update(update_id=update_id, callback_query=callback)


# --- /start -----------------------------------------------------------


async def test_start_command_sends_welcome_with_name_and_menu(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/start"))

    assert len(recording.calls) == 1
    sent = recording.calls[0]
    assert sent.__class__.__name__ == "SendMessage"
    assert "Alice" in sent.text
    assert sent.reply_markup is not None  # main menu keyboard attached


async def test_start_command_creates_user_row_with_default_language(db_engine, bot, dispatcher) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _message_update("/start", user_id=123))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        user = await UserRepository.get(session, 123)
        assert user is not None
        assert user.language_code == "uz"  # DEFAULT_LANGUAGE


async def test_start_command_shows_admin_button_for_admin_user(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/start", user_id=777))  # matches settings.ADMIN_IDS

    sent = recording.calls[0]
    admin_translator = Translator("uz")
    all_button_texts = [btn.text for row in sent.reply_markup.keyboard for btn in row]
    assert admin_translator.t("menu_admin") in all_button_texts


async def test_start_command_hides_admin_button_for_regular_user(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/start", user_id=42))

    sent = recording.calls[0]
    admin_translator = Translator("uz")
    all_button_texts = [btn.text for row in sent.reply_markup.keyboard for btn in row]
    assert admin_translator.t("menu_admin") not in all_button_texts


# --- /help and the Help menu button ------------------------------------


async def test_help_command_sends_help_text(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/help"))

    assert len(recording.calls) == 1
    assert "Find Music" in recording.calls[0].text or "🎵" in recording.calls[0].text


@pytest.mark.parametrize("language", ["uz", "ru", "en"])
async def test_help_menu_button_matches_in_every_language(db_engine, bot, dispatcher, language: str) -> None:
    b, recording = bot
    label = Translator(language).t("menu_help")
    await dispatcher.feed_update(b, _message_update(label))

    assert len(recording.calls) == 1
    assert recording.calls[0].__class__.__name__ == "SendMessage"


# --- /language and the Language menu button -----------------------------


async def test_language_command_shows_language_keyboard(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/language"))

    assert len(recording.calls) == 1
    sent = recording.calls[0]
    assert sent.reply_markup is not None
    assert len(sent.reply_markup.inline_keyboard) == 3


async def test_language_selection_updates_db_and_sends_confirmation(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    callback_data = LanguageCallback(language="ru").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=55))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        user = await UserRepository.get(session, 55)
        assert user is not None
        assert user.language_code == "ru"

    method_names = [c.__class__.__name__ for c in recording.calls]
    assert "AnswerCallbackQuery" in method_names
    assert "EditMessageText" in method_names
    assert "SendMessage" in method_names  # the refreshed-menu follow-up message


async def test_language_selection_confirmation_is_in_the_new_language(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    callback_data = LanguageCallback(language="en").pack()
    await dispatcher.feed_update(b, _callback_update(callback_data, user_id=56))

    answer_call = next(c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery")
    assert answer_call.text == Translator("en").t("language_changed")


async def test_language_selection_malformed_callback_data_does_not_crash(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    # A callback_data with the right prefix but malformed structure.
    await dispatcher.feed_update(b, _callback_update("lang:not-a-real-language:extra:parts", user_id=57))

    # Must not raise, and must answer the callback (even if a no-op) so
    # Telegram's client doesn't show a perpetual loading spinner.
    assert any(c.__class__.__name__ == "AnswerCallbackQuery" for c in recording.calls)
