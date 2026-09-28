"""Tests for app/workers/tasks.py's broadcast_job.

Runs against a REAL in-memory SQLite DB (via the app's own session module,
same pattern as the live Postgres validation performed during development —
see ARCHITECTURE.md for why sessions/repositories are tested this way) with
only the Bot's send_message calls faked, so we can script specific per-user
failure modes (blocked bot, flood control) without a live Telegram chat.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base
from app.db.repositories import BroadcastRepository, UserRepository
from app.services.downloader.manager import DownloadManager
from app.services.media.ffmpeg_tools import MediaTools
from app.services.recognition.factory import get_recognition_provider
from app.services.storage.factory import get_storage_backend
from app.workers.context import WorkerContext
from app.workers.tasks import broadcast_job


@pytest.fixture(autouse=True)
async def reset_db_engine():
    await db_session_module.dispose_engine()
    yield
    await db_session_module.dispose_engine()


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
        RECOGNITION_PROVIDER="audd",
        AUDD_API_TOKEN="test",
        STORAGE_BACKEND="local",
        PUBLIC_BASE_URL="http://localhost:8080",
    )


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def worker_ctx(settings: Settings, fake_redis, tmp_path) -> WorkerContext:
    settings.WORKDIR = str(tmp_path)
    mock_bot = AsyncMock()
    return WorkerContext(
        settings=settings,
        bot=mock_bot,
        sessionmaker=db_session_module.get_sessionmaker(),
        redis=fake_redis,
        recognition_provider=get_recognition_provider(settings),
        download_manager=DownloadManager(settings),
        media_tools=MediaTools(timeout_seconds=60),
        storage_backend=get_storage_backend(settings),
    )


async def _seed_users(session, user_ids: list[int], banned_ids: set[int] | None = None) -> None:
    banned_ids = banned_ids or set()
    for uid in user_ids:
        await UserRepository.get_or_create(session, user_id=uid, username=f"u{uid}", first_name="X", default_language="en")
        if uid in banned_ids:
            await UserRepository.set_banned(session, uid, True)
    await session.commit()


async def test_broadcast_sends_to_all_non_banned_users(db_engine, worker_ctx) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await _seed_users(session, [1, 2, 3], banned_ids={3})
        await BroadcastRepository.create(session, broadcast_id="b1", admin_id=99, message_text="Hello!")
        await session.commit()

    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="b1")

    sent_to = [call.kwargs["chat_id"] for call in worker_ctx.bot.send_message.call_args_list]
    assert sorted(sent_to) == [1, 2]  # user 3 excluded (banned)
    assert all(call.kwargs["text"] == "Hello!" for call in worker_ctx.bot.send_message.call_args_list)


async def test_broadcast_updates_progress_and_marks_completed(db_engine, worker_ctx) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await _seed_users(session, [10, 11])
        await BroadcastRepository.create(session, broadcast_id="b2", admin_id=99, message_text="Update!")
        await session.commit()

    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="b2")

    async with sm() as session:
        broadcast = await BroadcastRepository.get(session, "b2")
        assert broadcast.status == "completed"
        assert broadcast.total_users == 2
        assert broadcast.sent_count == 2
        assert broadcast.failed_count == 0
        assert broadcast.completed_at is not None


async def test_broadcast_counts_forbidden_as_failed_not_fatal(db_engine, worker_ctx) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await _seed_users(session, [20, 21, 22])
        await BroadcastRepository.create(session, broadcast_id="b3", admin_id=99, message_text="Hi!")
        await session.commit()

    async def fake_send(chat_id, text):
        if chat_id == 21:
            raise TelegramForbiddenError(method=None, message="Forbidden: bot was blocked by the user")
        return True

    worker_ctx.bot.send_message = fake_send

    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="b3")  # must not raise

    async with sm() as session:
        broadcast = await BroadcastRepository.get(session, "b3")
        assert broadcast.status == "completed"
        assert broadcast.sent_count == 2
        assert broadcast.failed_count == 1


async def test_broadcast_retries_after_flood_control_and_succeeds(db_engine, worker_ctx) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await _seed_users(session, [30])
        await BroadcastRepository.create(session, broadcast_id="b4", admin_id=99, message_text="Hi!")
        await session.commit()

    call_count = {"n": 0}

    async def fake_send(chat_id, text):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0)
        return True

    worker_ctx.bot.send_message = fake_send

    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="b4")

    assert call_count["n"] == 2  # first raised RetryAfter, second (the retry) succeeded
    async with sm() as session:
        broadcast = await BroadcastRepository.get(session, "b4")
        assert broadcast.sent_count == 1
        assert broadcast.failed_count == 0


async def test_broadcast_counts_persistent_retry_after_failure(db_engine, worker_ctx) -> None:
    """If even the one retry-after-sleeping attempt fails again, that user is
    counted as failed rather than retried indefinitely (bounded by design —
    an infinite retry loop would let one stuck recipient block the entire
    broadcast)."""
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await _seed_users(session, [40])
        await BroadcastRepository.create(session, broadcast_id="b5", admin_id=99, message_text="Hi!")
        await session.commit()

    async def always_flood(chat_id, text):
        raise TelegramRetryAfter(method=None, message="Too Many Requests", retry_after=0)

    worker_ctx.bot.send_message = always_flood

    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="b5")

    async with sm() as session:
        broadcast = await BroadcastRepository.get(session, "b5")
        assert broadcast.sent_count == 0
        assert broadcast.failed_count == 1


async def test_broadcast_with_no_users_completes_cleanly(db_engine, worker_ctx) -> None:
    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        await BroadcastRepository.create(session, broadcast_id="b6", admin_id=99, message_text="Hi!")
        await session.commit()

    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="b6")

    async with sm() as session:
        broadcast = await BroadcastRepository.get(session, "b6")
        assert broadcast.status == "completed"
        assert broadcast.total_users == 0


async def test_broadcast_with_unknown_id_does_not_raise(db_engine, worker_ctx) -> None:
    await broadcast_job({"worker_ctx": worker_ctx}, broadcast_id="does-not-exist")  # must not raise
    worker_ctx.bot.send_message.assert_not_called()
