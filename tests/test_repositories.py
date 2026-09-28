"""Tests for app/db/repositories.py against an in-memory SQLite DB."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    BotSettingRepository,
    CaptionRepository,
    JobRepository,
    PlatformRepository,
    StatsRepository,
    UserRepository,
)

pytestmark = pytest.mark.asyncio


async def test_user_get_or_create_then_update(db_session: AsyncSession) -> None:
    user = await UserRepository.get_or_create(
        db_session, user_id=42, username="alice", first_name="Alice", default_language="uz"
    )
    assert user.id == 42
    assert user.language_code == "uz"
    await db_session.commit()

    # Second call updates username/first_name but keeps the existing language.
    user2 = await UserRepository.get_or_create(
        db_session, user_id=42, username="alice2", first_name="Alice B", default_language="ru"
    )
    assert user2.username == "alice2"
    assert user2.language_code == "uz"  # unchanged, not overwritten by default_language


async def test_user_set_language_and_banned(db_session: AsyncSession) -> None:
    await UserRepository.get_or_create(
        db_session, user_id=1, username=None, first_name=None, default_language="uz"
    )
    await UserRepository.set_language(db_session, 1, "ru")
    await UserRepository.set_banned(db_session, 1, True)
    await db_session.commit()

    user = await UserRepository.get(db_session, 1)
    assert user is not None
    assert user.language_code == "ru"
    assert user.is_banned is True


async def test_job_lifecycle(db_session: AsyncSession) -> None:
    await UserRepository.get_or_create(
        db_session, user_id=7, username=None, first_name=None, default_language="uz"
    )
    job = await JobRepository.create(
        db_session, job_id="job-1", user_id=7, job_type="recognize", platform=None, source_url=None
    )
    assert job.status == "pending"

    await JobRepository.mark_processing(db_session, "job-1")
    fetched = await JobRepository.get(db_session, "job-1")
    assert fetched is not None
    assert fetched.status == "processing"

    await JobRepository.mark_completed(db_session, "job-1", result_meta={"title": "Song"})
    fetched = await JobRepository.get(db_session, "job-1")
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.result_meta == {"title": "Song"}


async def test_job_mark_failed_truncates_long_error(db_session: AsyncSession) -> None:
    await UserRepository.get_or_create(
        db_session, user_id=8, username=None, first_name=None, default_language="uz"
    )
    await JobRepository.create(db_session, job_id="job-2", user_id=8, job_type="download")
    await JobRepository.mark_failed(db_session, "job-2", "x" * 3000)

    fetched = await JobRepository.get(db_session, "job-2")
    assert fetched is not None
    assert fetched.status == "failed"
    assert fetched.error_message is not None
    assert len(fetched.error_message) == 2000


async def test_caption_template_default_fallback(db_session: AsyncSession) -> None:
    # No row inserted yet -> falls back to the built-in default template.
    template = await CaptionRepository.get_template(db_session, "audio")
    assert "{title}" in template
    assert "{artist}" in template


async def test_caption_template_set_and_get(db_session: AsyncSession) -> None:
    await CaptionRepository.set_template(db_session, "video", "Custom: {title}", updated_by=1)
    await db_session.commit()
    template = await CaptionRepository.get_template(db_session, "video")
    assert template == "Custom: {title}"


async def test_caption_buttons_add_list_remove(db_session: AsyncSession) -> None:
    b1 = await CaptionRepository.add_button(db_session, label="Channel", url="https://t.me/x", media_type="video", position=0)
    b2 = await CaptionRepository.add_button(db_session, label="All types", url="https://t.me/y", media_type=None, position=1)
    await db_session.commit()

    video_buttons = await CaptionRepository.list_buttons(db_session, "video")
    assert {b.id for b in video_buttons} == {b1.id, b2.id}

    audio_buttons = await CaptionRepository.list_buttons(db_session, "audio")
    assert [b.id for b in audio_buttons] == [b2.id]  # only the "all types" button applies

    await CaptionRepository.remove_button(db_session, b1.id)
    await db_session.commit()
    video_buttons_after = await CaptionRepository.list_buttons(db_session, "video")
    assert [b.id for b in video_buttons_after] == [b2.id]


async def test_platform_enabled_default_true_when_unset(db_session: AsyncSession) -> None:
    assert await PlatformRepository.is_enabled(db_session, "youtube") is True


async def test_platform_disable_and_enable(db_session: AsyncSession) -> None:
    await PlatformRepository.set_enabled(db_session, "tiktok", False)
    await db_session.commit()
    assert await PlatformRepository.is_enabled(db_session, "tiktok") is False

    await PlatformRepository.set_enabled(db_session, "tiktok", True)
    await db_session.commit()
    assert await PlatformRepository.is_enabled(db_session, "tiktok") is True


async def test_platform_max_file_size_override(db_session: AsyncSession) -> None:
    assert await PlatformRepository.get_max_file_size_mb(db_session, "pinterest") is None
    await PlatformRepository.set_max_file_size_mb(db_session, "pinterest", 200)
    await db_session.commit()
    assert await PlatformRepository.get_max_file_size_mb(db_session, "pinterest") == 200


async def test_bot_setting_get_default_and_set(db_session: AsyncSession) -> None:
    assert await BotSettingRepository.get(db_session, "rate_limit_download", default=5) == 5
    await BotSettingRepository.set(db_session, "rate_limit_download", 10, updated_by=99)
    await db_session.commit()
    assert await BotSettingRepository.get(db_session, "rate_limit_download") == 10


async def test_stats_repository_counts(db_session: AsyncSession) -> None:
    await UserRepository.get_or_create(db_session, user_id=1, username=None, first_name=None, default_language="uz")
    await UserRepository.get_or_create(db_session, user_id=2, username=None, first_name=None, default_language="ru")
    await JobRepository.create(db_session, job_id="j1", user_id=1, job_type="recognize", platform=None)
    await JobRepository.create(db_session, job_id="j2", user_id=1, job_type="download", platform="tiktok")
    await JobRepository.mark_completed(db_session, "j1", None)
    await JobRepository.mark_failed(db_session, "j2", "boom")
    await db_session.commit()

    stats = await StatsRepository.compute(db_session)
    assert stats.total_users == 2
    assert stats.jobs_total == 2
    assert stats.jobs_completed == 1
    assert stats.jobs_failed == 1
    assert stats.jobs_by_type == {"recognize": 1, "download": 1}
    assert stats.jobs_by_platform == {"tiktok": 1}
