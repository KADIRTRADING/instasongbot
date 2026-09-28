"""Test for app/bot/middlewares/logging.py using structlog's own testing
helpers to capture emitted log events, against a real Dispatcher dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.middlewares.logging import LoggingMiddleware


class FakeSession(BaseSession):
    async def close(self) -> None:
        pass

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        return True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
async def bot():
    b = Bot(token="123456:ABCDEF_fake_test_token", session=FakeSession())
    yield b
    await b.session.close()


async def test_logging_middleware_binds_context_and_logs_completion(bot: Bot) -> None:
    """`structlog.testing.capture_logs()` swaps in its own processor chain
    (it does NOT run `merge_contextvars`), so contextvars bound via
    `bind_contextvars` deliberately don't appear in `captured` here — that's
    documented structlog test behavior, not a bug in the middleware. This
    test asserts what capture_logs CAN observe (direct log-call kwargs);
    the contextvars binding itself is asserted against the real configured
    renderer in the next test, which checks actual emitted output.
    """
    dp = Dispatcher()
    dp.update.outer_middleware(LoggingMiddleware())

    router = Router()

    @router.message()
    async def handler(message: Message) -> None:
        pass

    dp.include_router(router)

    chat = Chat(id=1, type="private")
    tg_user = TgUser(id=555, is_bot=False, first_name="T")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text="hi")
    update = Update(update_id=777, message=message)

    with structlog.testing.capture_logs() as captured:
        await dp.feed_update(bot, update)

    completion_events = [e for e in captured if e.get("event") == "update_handled"]
    assert len(completion_events) == 1
    entry = completion_events[0]
    assert entry["event_type"] == "message"
    assert "duration_ms" in entry


async def test_logging_middleware_emits_update_id_and_user_id_via_real_renderer(bot: Bot, capsys) -> None:
    """Configures the REAL logging pipeline (app.logging_conf.configure_logging,
    JSON renderer) so contextvars merging genuinely runs, then checks stdout
    for the bound fields -- this is what actually happens in production.
    """
    from app.config import Settings
    from app.logging_conf import configure_logging

    configure_logging(Settings(BOT_TOKEN="x", DATABASE_URL="sqlite+aiosqlite:///:memory:", LOG_JSON=True))

    dp = Dispatcher()
    dp.update.outer_middleware(LoggingMiddleware())

    router = Router()

    @router.message()
    async def handler(message: Message) -> None:
        pass

    dp.include_router(router)

    chat = Chat(id=1, type="private")
    tg_user = TgUser(id=555, is_bot=False, first_name="T")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text="hi")
    update = Update(update_id=777, message=message)

    await dp.feed_update(bot, update)

    import json

    captured_out = capsys.readouterr().out
    lines = [line for line in captured_out.splitlines() if "update_handled" in line]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["update_id"] == 777
    assert payload["user_id"] == 555
    assert payload["event_type"] == "message"


async def test_logging_middleware_logs_even_when_handler_raises(bot: Bot) -> None:
    dp = Dispatcher()
    dp.update.outer_middleware(LoggingMiddleware())
    dp.errors.register(lambda event, **kw: None)  # swallow so feed_update doesn't propagate

    router = Router()

    @router.message()
    async def handler(message: Message) -> None:
        raise RuntimeError("boom")

    dp.include_router(router)

    chat = Chat(id=1, type="private")
    tg_user = TgUser(id=1, is_bot=False, first_name="T")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text="hi")
    update = Update(update_id=1, message=message)

    with structlog.testing.capture_logs() as captured:
        await dp.feed_update(bot, update)

    # The `finally` block in LoggingMiddleware must run even though the
    # handler raised (and was subsequently caught by the errors middleware).
    completion_events = [e for e in captured if e.get("event") == "update_handled"]
    assert len(completion_events) == 1
