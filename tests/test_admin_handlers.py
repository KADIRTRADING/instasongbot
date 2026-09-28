"""Tests for app/bot/handlers/admin.py, exercising the REAL aiogram
Dispatcher end-to-end (feed_update) with the real DbSessionMiddleware +
UserContextMiddleware stack (so `is_admin` gating is genuine, not mocked),
a real in-memory SQLite DB, and aiogram's own built-in FSM middleware (real
MemoryStorage, not mocked) so multi-step flows are exercised exactly as they
run in production. Only the Bot's HTTP session and the arq pool are faked.
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

from app.bot.handlers.admin import router as admin_router
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.user_context import UserContextMiddleware
from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import (
    BotSettingRepository,
    BroadcastRepository,
    CaptionRepository,
    PlatformRepository,
)
from app.i18n.translator import Translator

ADMIN_ID = 777
NON_ADMIN_ID = 1


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
    # See test_core_handlers.py's docstring on why this reset is needed:
    # aiogram Routers are one-parent-only, and `admin_router` is a
    # module-level singleton re-used across every test's fresh Dispatcher.
    yield
    admin_router._parent_router = None


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
    return Settings(BOT_TOKEN="123456:fake", DATABASE_URL="sqlite+aiosqlite:///:memory:", ADMIN_IDS=str(ADMIN_ID))


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
    pool = AsyncMock()
    return pool


@pytest.fixture
def dispatcher(settings: Settings, mock_arq_pool: AsyncMock) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings  # matches build_dispatcher(); the Bot Settings handlers need it
    dp["arq_pool"] = mock_arq_pool
    dp.update.outer_middleware(DbSessionMiddleware())
    dp.update.outer_middleware(UserContextMiddleware(settings))
    dp.include_router(admin_router)
    return dp


def _message_update(text: str, *, user_id: int = ADMIN_ID, update_id: int = 1) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    message = Message(message_id=1, date=0, chat=chat, from_user=tg_user, text=text)
    return Update(update_id=update_id, message=message)


def _callback_update(data: str, *, user_id: int = ADMIN_ID, update_id: int = 1, message_id: int = 5) -> Update:
    chat = Chat(id=999, type="private")
    tg_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    message = Message(message_id=message_id, date=0, chat=chat, from_user=tg_user, text="placeholder")
    callback = CallbackQuery(id="cb1", from_user=tg_user, chat_instance="x", data=data, message=message)
    return Update(update_id=update_id, callback_query=callback)


def _edit_texts(recording: RecordingSession) -> list[str]:
    return [c.text for c in recording.calls if c.__class__.__name__ == "EditMessageText"]


def _sent_texts(recording: RecordingSession) -> list[str]:
    return [c.text for c in recording.calls if c.__class__.__name__ == "SendMessage"]


# --- Gating: only admins reach this router at all --------------------------


async def test_admin_command_rejected_for_non_admin(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/admin", user_id=NON_ADMIN_ID))

    assert recording.calls == []  # router-level filter blocked it entirely


async def test_admin_command_shows_menu_for_admin(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _message_update("/admin"))

    assert len(recording.calls) == 1
    sent = recording.calls[0]
    assert sent.__class__.__name__ == "SendMessage"
    assert sent.text == Translator("uz").t("admin_menu_title")
    assert sent.reply_markup is not None
    # captions/buttons/platforms/stats/limits/settings/broadcast
    assert len(sent.reply_markup.inline_keyboard) == 7


async def test_non_admin_callback_ignored(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:captions", user_id=NON_ADMIN_ID))

    assert recording.calls == []


async def test_back_to_menu_callback_returns_to_root_menu(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:menu"))

    method_names = [c.__class__.__name__ for c in recording.calls]
    assert "AnswerCallbackQuery" in method_names
    assert "EditMessageText" in method_names
    assert _edit_texts(recording)[0] == Translator("uz").t("admin_menu_title")


# --- Captions ----------------------------------------------------------


async def test_captions_menu_shows_media_type_picker(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:captions"))

    edited = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"]
    assert len(edited) == 1
    assert edited[0].text == Translator("uz").t("admin_pick_media_type")
    assert len(edited[0].reply_markup.inline_keyboard) == 4  # video/audio/image + back


async def test_selecting_media_type_prompts_for_template_text(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:capmedia:audio"))

    edited = _edit_texts(recording)
    assert len(edited) == 1
    assert "audio" in edited[0].lower() or Translator("uz").t("media_type_audio") in edited[0]


async def test_full_caption_edit_flow_saves_template(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:capmedia:video", update_id=1))
    await dispatcher.feed_update(b, _message_update("New template: {title} by {artist}", update_id=2))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        template = await CaptionRepository.get_template(session, "video")
    assert template == "New template: {title} by {artist}"

    sent = _sent_texts(recording)
    assert Translator("uz").t("admin_saved") in sent
    assert any("Sample Title" in t for t in sent)  # rendered preview


async def test_caption_flow_cancel_returns_to_menu_without_saving(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:capmedia:image", update_id=1))
    await dispatcher.feed_update(b, _message_update("/cancel", update_id=2))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        template = await CaptionRepository.get_template(session, "image")
    assert template != "/cancel"  # never persisted

    sent = _sent_texts(recording)
    assert Translator("uz").t("admin_operation_cancelled") in sent


async def test_invalid_media_type_in_callback_is_ignored(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:capmedia:not-a-real-type"))

    # Only an AnswerCallbackQuery (the early-return `callback.answer()`), no
    # EditMessageText -- malformed data must not crash or proceed.
    assert not _edit_texts(recording)


# --- Buttons -------------------------------------------------------------


async def test_buttons_menu_shows_empty_state(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:buttons"))

    edited = _edit_texts(recording)
    assert edited[0] == Translator("uz").t("admin_no_buttons_yet")


async def test_full_add_button_flow_creates_row(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:addbtn", update_id=1))
    await dispatcher.feed_update(b, _callback_update("adm:btnmedia:video", update_id=2))
    await dispatcher.feed_update(b, _message_update("Our Channel", update_id=3))
    await dispatcher.feed_update(b, _message_update("https://t.me/somechannel", update_id=4))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        buttons = await CaptionRepository.list_all_buttons(session)
    assert len(buttons) == 1
    assert buttons[0].label == "Our Channel"
    assert buttons[0].url == "https://t.me/somechannel"
    assert buttons[0].media_type == "video"

    sent = _sent_texts(recording)
    assert any("Our Channel" in t for t in sent)


async def test_add_button_all_media_types_stores_null_media_type(db_engine, bot, dispatcher) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _callback_update("adm:addbtn", update_id=1))
    await dispatcher.feed_update(b, _callback_update("adm:btnmedia:all", update_id=2))
    await dispatcher.feed_update(b, _message_update("Support", update_id=3))
    await dispatcher.feed_update(b, _message_update("https://t.me/support", update_id=4))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        buttons = await CaptionRepository.list_all_buttons(session)
    assert len(buttons) == 1
    assert buttons[0].media_type is None


async def test_add_button_rejects_invalid_url_and_stays_in_flow(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:addbtn", update_id=1))
    await dispatcher.feed_update(b, _callback_update("adm:btnmedia:video", update_id=2))
    await dispatcher.feed_update(b, _message_update("My Label", update_id=3))
    await dispatcher.feed_update(b, _message_update("not-a-url", update_id=4))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        buttons = await CaptionRepository.list_all_buttons(session)
    assert buttons == []  # never created -- invalid URL rejected

    sent = _sent_texts(recording)
    assert Translator("uz").t("admin_invalid_url") in sent

    # The flow should still be alive: sending a valid URL now should succeed.
    await dispatcher.feed_update(b, _message_update("https://example.com/ok", update_id=5))
    async with sm() as session:
        buttons_after = await CaptionRepository.list_all_buttons(session)
    assert len(buttons_after) == 1
    assert buttons_after[0].url == "https://example.com/ok"


async def test_remove_button_deletes_row(db_engine, bot, dispatcher) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        button = await CaptionRepository.add_button(session, label="X", url="https://x.com", media_type=None, position=0)
        await session.commit()
        button_id = button.id

    b, recording = bot
    await dispatcher.feed_update(b, _callback_update(f"adm:rmbtn:{button_id}"))

    async with sm() as session:
        buttons = await CaptionRepository.list_all_buttons(session)
    assert buttons == []

    answer_calls = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert any(c.text == Translator("uz").t("admin_button_removed") for c in answer_calls)


# --- Platforms -----------------------------------------------------------


async def test_platforms_menu_shows_all_platforms_enabled_by_default(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:platforms"))

    edited = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"][0]
    # 6 platforms + back row
    assert len(edited.reply_markup.inline_keyboard) == 7
    assert all("✅" in row[0].text for row in edited.reply_markup.inline_keyboard[:-1])


async def test_toggle_platform_disables_then_reenables(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:toggleplat:tiktok", update_id=1))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        assert await PlatformRepository.is_enabled(session, "tiktok") is False

    answer_calls = [c for c in recording.calls if c.__class__.__name__ == "AnswerCallbackQuery"]
    assert Translator("uz").t("admin_state_disabled") in answer_calls[0].text

    await dispatcher.feed_update(b, _callback_update("adm:toggleplat:tiktok", update_id=2))
    async with sm() as session:
        assert await PlatformRepository.is_enabled(session, "tiktok") is True


async def test_toggle_invalid_platform_ignored(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:toggleplat:not-a-platform"))

    assert not _edit_texts(recording)


# --- Stats -----------------------------------------------------------------


async def test_stats_shows_computed_numbers(db_engine, bot, dispatcher) -> None:
    from app.db.repositories import JobRepository, UserRepository

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await UserRepository.get_or_create(session, user_id=1001, username=None, first_name=None, default_language="en")
        await JobRepository.create(session, job_id="j1", user_id=1001, job_type="download", platform="tiktok")
        await JobRepository.mark_completed(session, "j1", None)
        await session.commit()

    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:stats"))

    edited = _edit_texts(recording)
    assert len(edited) == 1
    assert "1" in edited[0]  # total_users / jobs_total appear somewhere
    assert "TikTok" in edited[0]  # platform names are translated for display, not raw keys


# --- Limits ----------------------------------------------------------------


async def test_limits_menu_shows_picker(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:limits"))

    edited = _edit_texts(recording)
    assert edited[0] == Translator("uz").t("admin_pick_limit_target")


async def test_set_platform_limit_full_flow(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:setlimit:youtube", update_id=1))
    await dispatcher.feed_update(b, _message_update("250", update_id=2))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        limit = await PlatformRepository.get_max_file_size_mb(session, "youtube")
    assert limit == 250

    sent = _sent_texts(recording)
    assert any("250" in t for t in sent)


async def test_set_global_limit_full_flow(db_engine, bot, dispatcher) -> None:
    b, _ = bot
    await dispatcher.feed_update(b, _callback_update("adm:setlimit:global", update_id=1))
    await dispatcher.feed_update(b, _message_update("1000", update_id=2))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        value = await BotSettingRepository.get(session, "max_download_mb")
    assert value == 1000


async def test_limit_value_rejects_non_integer(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:setlimit:youtube", update_id=1))
    await dispatcher.feed_update(b, _message_update("not-a-number", update_id=2))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        limit = await PlatformRepository.get_max_file_size_mb(session, "youtube")
    assert limit is None  # never set

    sent = _sent_texts(recording)
    assert Translator("uz").t("admin_invalid_number") in sent


async def test_limit_value_rejects_zero_and_negative(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:setlimit:youtube", update_id=1))
    await dispatcher.feed_update(b, _message_update("0", update_id=2))
    await dispatcher.feed_update(b, _message_update("-5", update_id=3))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        limit = await PlatformRepository.get_max_file_size_mb(session, "youtube")
    assert limit is None

    sent = _sent_texts(recording)
    assert sent.count(Translator("uz").t("admin_invalid_number")) == 2


# --- Broadcast --------------------------------------------------------------


async def test_broadcast_full_flow_confirm_enqueues_job(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    from app.db.repositories import UserRepository

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await UserRepository.get_or_create(session, user_id=555, username=None, first_name=None, default_language="en")
        await session.commit()

    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:broadcast", update_id=1))
    await dispatcher.feed_update(b, _message_update("Big announcement!", update_id=2))

    sent = _sent_texts(recording)
    assert any("Big announcement!" in t for t in sent)  # preview shown

    await dispatcher.feed_update(b, _callback_update("adm:bcastconfirm", update_id=3))

    mock_arq_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_arq_pool.enqueue_job.call_args
    assert args[0] == "broadcast_job"
    broadcast_id = kwargs["broadcast_id"]

    async with sm() as session:
        broadcast = await BroadcastRepository.get(session, broadcast_id)
    assert broadcast is not None
    assert broadcast.message_text == "Big announcement!"
    assert broadcast.admin_id == ADMIN_ID


async def test_broadcast_cancel_does_not_enqueue(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:broadcast", update_id=1))
    await dispatcher.feed_update(b, _message_update("Never sent", update_id=2))
    await dispatcher.feed_update(b, _callback_update("adm:bcastcancel", update_id=3))

    mock_arq_pool.enqueue_job.assert_not_called()
    # The cancellation confirmation is an edit of the confirm-prompt message,
    # not a new SendMessage (see on_broadcast_cancel's `edit_text` call).
    assert Translator("uz").t("admin_broadcast_cancelled_by_admin") in _edit_texts(recording)


async def test_broadcast_confirm_without_prior_text_uses_empty_string(db_engine, bot, dispatcher, mock_arq_pool) -> None:
    """Defensive: confirming without the state machine ever having recorded
    text (shouldn't be reachable via the real keyboard flow, but the
    callback itself doesn't re-validate) must not crash.
    """
    b, _ = bot
    await dispatcher.feed_update(b, _callback_update("adm:broadcast", update_id=1))
    # Skip straight to confirm without sending broadcast text first --
    # state is still "waiting_for_text", so bcastconfirm's StateFilter
    # (waiting_for_confirmation) won't even match, proving it's a no-op
    # rather than a crash.
    await dispatcher.feed_update(b, _callback_update("adm:bcastconfirm", update_id=2))

    mock_arq_pool.enqueue_job.assert_not_called()



# --- Bot settings (auto quality / result buttons) ----------------------------


async def test_settings_menu_shows_current_quality_and_buttons(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:settings"))

    edited = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"]
    assert len(edited) == 1
    # Two setting rows + a back row.
    assert len(edited[0].reply_markup.inline_keyboard) == 3


async def test_pick_quality_shows_all_options(db_engine, bot, dispatcher) -> None:
    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:setquality"))

    edited = [c for c in recording.calls if c.__class__.__name__ == "EditMessageText"]
    assert edited[0].text == Translator("uz").t("admin_pick_quality")
    # best/720/480/audio + back
    assert len(edited[0].reply_markup.inline_keyboard) == 5


async def test_selecting_quality_persists_to_bot_settings(db_engine, bot, dispatcher) -> None:
    from app.bot.settings_store import get_auto_video_quality

    b, _ = bot
    await dispatcher.feed_update(b, _callback_update("adm:quality:480"))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        assert await get_auto_video_quality(session, env_default="best") == "480"


async def test_toggling_result_buttons_flips_and_persists(db_engine, bot, dispatcher) -> None:
    from app.bot.settings_store import get_show_result_buttons

    b, _ = bot
    # default is True -> toggling once turns it off
    await dispatcher.feed_update(b, _callback_update("adm:togglebtns"))

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        assert await get_show_result_buttons(session) is False


async def test_non_admin_cannot_change_settings(db_engine, bot, dispatcher) -> None:
    from app.bot.settings_store import get_auto_video_quality

    b, recording = bot
    await dispatcher.feed_update(b, _callback_update("adm:quality:audio", user_id=NON_ADMIN_ID))

    assert recording.calls == []  # router-level is_admin gate blocked it
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        # Unchanged (no override written).
        assert await get_auto_video_quality(session, env_default="best") == "best"
