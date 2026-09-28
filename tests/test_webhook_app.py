"""Tests for app/webhook_app.py using aiohttp's real test server (a genuine
TCP server on a random port, real HTTP requests — not mocked, same pattern
as tests/test_file_server.py) with only Redis (fakeredis), the arq pool
(AsyncMock), and the Bot's own outbound HTTP session faked out.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiohttp.test_utils import TestClient, TestServer

from app.bot.handlers import admin, convert, core, download, recognize
from app.config import Settings
from app.db import session as db_session_module
from app.db.base import Base

WEBHOOK_SECRET = "test-webhook-secret"


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
def reset_router_parents():
    # build_dispatcher() (called inside create_webhook_app) includes every
    # module-level router singleton -- see tests/test_dispatcher.py for why
    # this reset is required between tests.
    yield
    for module in (admin, core, recognize, convert, download):
        module.router._parent_router = None


def _settings(tmp_path) -> Settings:
    return Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        WEBHOOK_BASE_URL="https://bot.example.com",
        WEBHOOK_PATH="/webhook",
        WEBHOOK_SECRET=WEBHOOK_SECRET,
        STORAGE_BACKEND="local",
        WORKDIR=str(tmp_path),
        PUBLIC_BASE_URL="https://bot.example.com",
    )


@pytest.fixture
async def app_client(tmp_path):
    """Builds the real webhook aiohttp Application (create_webhook_app) with
    Redis/arq faked, then wraps it in aiohttp's real TestServer/TestClient --
    exercising genuine HTTP request/response handling, not a mocked app.

    The schema is created against the SAME sqlite+aiosqlite:///:memory: URL
    create_webhook_app's own `init_engine(settings)` call will use --
    `init_engine` is idempotent (a no-op if `_engine` is already set, see
    app/db/session.py), so seeding it here first means the dispatcher's real
    DbSessionMiddleware/UserContextMiddleware queries hit a real schema
    instead of an empty in-memory DB with no tables.
    """
    settings = _settings(tmp_path)
    engine = db_session_module.init_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    recording_session = RecordingSession()
    fake_bot = Bot(token="123456:ABCDEF_fake_test_token", session=recording_session)

    def fake_from_url(url: str):
        return fakeredis.aioredis.FakeRedis()

    mock_arq_pool = AsyncMock()
    mock_arq_pool.aclose = AsyncMock()

    async def fake_create_pool(redis_settings):
        return mock_arq_pool

    with (
        patch("app.webhook_app.Redis.from_url", side_effect=fake_from_url),
        patch("app.webhook_app.create_pool", side_effect=fake_create_pool),
        patch("app.webhook_app.build_bot", return_value=fake_bot),
    ):
        from app.webhook_app import create_webhook_app

        app = await create_webhook_app(settings)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        try:
            yield client, recording_session, mock_arq_pool
        finally:
            await client.close()


def _telegram_message_update(update_id: int = 1, text: str = "/start") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
            "text": text,
        },
    }


# --- Secret token validation -------------------------------------------


async def test_webhook_rejects_missing_secret(app_client) -> None:
    client, _, _ = app_client
    resp = await client.post("/webhook", json=_telegram_message_update())
    assert resp.status == 401


async def test_webhook_rejects_wrong_secret(app_client) -> None:
    client, _, _ = app_client
    resp = await client.post(
        "/webhook", json=_telegram_message_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
    )
    assert resp.status == 401


async def test_webhook_accepts_correct_secret_and_dispatches_update(app_client) -> None:
    client, recording, _ = app_client
    resp = await client.post(
        "/webhook",
        json=_telegram_message_update(text="/start"),
        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
    )
    assert resp.status == 200

    # aiogram's SimpleRequestHandler defaults to handle_in_background=True:
    # it acknowledges Telegram's POST immediately and processes the update
    # in a separate asyncio task (the standard, recommended aiogram webhook
    # pattern -- Telegram itself only waits ~60s for a response and retries
    # aggressively otherwise). The response landing doesn't guarantee the
    # background task has finished, so poll briefly for its side effect
    # rather than asserting immediately after the HTTP response.
    for _ in range(50):
        if any(c.__class__.__name__ == "SendMessage" for c in recording.calls):
            break
        await asyncio.sleep(0.02)

    # /start reaches core.cmd_start and sends a welcome message -- proof the
    # full dispatcher (routers + middleware stack) is wired and reachable
    # through the real HTTP request path, not just constructed.
    send_message_calls = [c for c in recording.calls if c.__class__.__name__ == "SendMessage"]
    assert len(send_message_calls) == 1
    assert "Alice" in send_message_calls[0].text


# --- Default rows are seeded on startup ----------------------------------


async def test_seed_defaults_runs_on_startup(app_client) -> None:
    """app/db/seed.py's seed_defaults() must run as part of building the
    app -- confirmed here by checking its rows exist, rather than mocking
    seed_defaults itself and asserting it was called (that would only prove
    the call site exists, not that it ran against a real, usable session)."""
    from app.constants import Platform
    from app.db import session as db_session_module
    from app.db.models import CaptionTemplate, PlatformSetting

    sm = db_session_module.get_sessionmaker()
    async with sm() as session:
        for media_type in ("video", "audio", "image"):
            row = await session.get(CaptionTemplate, media_type)
            assert row is not None, f"seed_defaults did not create a {media_type} caption template"

        for platform in Platform:
            row = await session.get(PlatformSetting, platform.value)
            assert row is not None, f"seed_defaults did not create a platform_settings row for {platform.value}"
            assert row.enabled is True


# --- Webhook registration on startup ------------------------------------


async def test_webhook_is_registered_with_telegram_on_startup(app_client) -> None:
    _, recording, _ = app_client
    set_webhook_calls = [c for c in recording.calls if c.__class__.__name__ == "SetWebhook"]
    assert len(set_webhook_calls) == 1
    assert set_webhook_calls[0].url == "https://bot.example.com/webhook"
    assert set_webhook_calls[0].secret_token == WEBHOOK_SECRET


# --- File-serving route is mounted alongside the webhook route ----------


async def test_file_route_is_mounted_when_storage_backend_is_local(app_client) -> None:
    client, _, _ = app_client
    # No sig/exp query params -> the file_server handler itself must reject
    # this (403), proving the route is registered and reachable, not a 404
    # from the router never having the path at all.
    resp = await client.get("/files/song.mp3")
    assert resp.status == 403


# --- Missing WEBHOOK_BASE_URL fails fast, not silently -------------------


async def test_create_webhook_app_requires_webhook_base_url(tmp_path) -> None:
    from app.webhook_app import create_webhook_app

    settings = Settings(
        BOT_TOKEN="123456:fake",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        WEBHOOK_BASE_URL="",
        WORKDIR=str(tmp_path),
    )
    with pytest.raises(RuntimeError, match="WEBHOOK_BASE_URL"):
        await create_webhook_app(settings)
