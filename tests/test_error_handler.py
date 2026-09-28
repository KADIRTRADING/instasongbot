"""Tests for app/bot/error_handler.py, exercising the REAL aiogram Dispatcher
dispatch/middleware/dependency-injection machinery end-to-end (not mocked),
with only the Bot's outbound HTTP session faked out (no real network call).
This is the strongest possible proof that `bot: Bot` really does get
injected into a `dp.errors`-registered handler by aiogram itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.error_handler import handle_unexpected_error


class RecordingSession(BaseSession):
    """A real aiogram BaseSession subclass (not a Mock) that records every
    method the Bot tried to call, without making a real HTTP request."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused here
        raise NotImplementedError


@pytest.fixture
async def bot_and_session():
    session = RecordingSession()
    bot = Bot(token="123456:ABCDEF_fake_test_token", session=session)
    yield bot, session
    await bot.session.close()


def _build_update(text: str = "hi") -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=42, is_bot=False, first_name="Test")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=1, message=message)


async def test_error_handler_catches_exception_and_notifies_user(bot_and_session) -> None:
    bot, session = bot_and_session
    dp = Dispatcher()
    dp.errors.register(handle_unexpected_error)

    router = Router()

    @router.message()
    async def boom_handler(message: Message) -> None:
        raise RuntimeError("deliberate test failure")

    dp.include_router(router)

    # feed_update must not raise -- the whole point of the error handler.
    result = await dp.feed_update(bot, _build_update())

    assert result is None
    assert len(session.calls) == 1
    sent = session.calls[0]
    assert sent.__class__.__name__ == "SendMessage"
    assert sent.chat_id == 999
    assert sent.text  # some translated error message was sent


async def test_error_handler_does_not_notify_when_no_chat_context() -> None:
    """An error occurring on an update type with no message/callback_query
    (e.g. a bare inline_query) has nowhere to send a notification -- the
    handler must return cleanly without trying to guess a chat_id."""
    from aiogram.types import ErrorEvent, InlineQuery
    from aiogram.types import User as TgUser2

    update = Update(update_id=2, inline_query=InlineQuery(id="q1", from_user=TgUser2(id=1, is_bot=False, first_name="X"), query="", offset=""))
    event = ErrorEvent(update=update, exception=RuntimeError("boom"))

    class DummySession(BaseSession):
        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            raise AssertionError("should never attempt to send a message here")

        async def stream_content(self, *a, **kw):
            raise NotImplementedError

    bot = Bot(token="123456:ABCDEF_fake_test_token_2", session=DummySession())
    try:
        await handle_unexpected_error(event, bot=bot)  # must not raise
    finally:
        await bot.session.close()


async def test_error_handler_swallows_its_own_notification_failure(bot_and_session) -> None:
    """If even sending the fallback error message fails (e.g. bot was
    blocked by the user), the error handler itself must not raise -- it's
    the last line of defense and has nowhere further to escalate to."""
    bot, session = bot_and_session

    async def failing_make_request(bot_arg, method, timeout=None):
        raise RuntimeError("Forbidden: bot was blocked by the user")

    session.make_request = failing_make_request  # type: ignore[method-assign]

    from aiogram.types import ErrorEvent

    event = ErrorEvent(update=_build_update(), exception=ValueError("original failure"))
    await handle_unexpected_error(event, bot=bot)  # must not raise
