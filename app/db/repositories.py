"""Repository layer: all queries live here so handlers/services never write raw
SQLAlchemy statements inline. Each repository takes an AsyncSession per call
(sessions are short-lived, request/job-scoped — see app/db/session.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Broadcast, CaptionButton, CaptionTemplate, Job, PlatformSetting, User

# NOTE: repositories below use a "get, then add-or-mutate" pattern rather than a
# dialect-specific upsert (e.g. Postgres ON CONFLICT). These tables are small,
# admin-configuration tables with no concurrent-write contention in practice (a
# human clicking "save" in the admin UI), so the extra round trip is a non-issue,
# and this way every repository method works identically against Postgres in
# production and SQLite in tests without maintaining two SQL dialects.


class UserRepository:
    @staticmethod
    async def get_or_create(
        session: AsyncSession,
        *,
        user_id: int,
        username: str | None,
        first_name: str | None,
        default_language: str,
    ) -> User:
        user = await session.get(User, user_id)
        if user is not None:
            user.username = username
            user.first_name = first_name
            return user
        user = User(
            id=user_id,
            username=username,
            first_name=first_name,
            language_code=default_language,
        )
        session.add(user)
        await session.flush()
        return user

    @staticmethod
    async def set_language(session: AsyncSession, user_id: int, language_code: str) -> None:
        user = await session.get(User, user_id)
        if user is not None:
            user.language_code = language_code

    @staticmethod
    async def set_banned(session: AsyncSession, user_id: int, banned: bool) -> None:
        user = await session.get(User, user_id)
        if user is not None:
            user.is_banned = banned

    @staticmethod
    async def get(session: AsyncSession, user_id: int) -> User | None:
        return await session.get(User, user_id)

    @staticmethod
    async def count_all(session: AsyncSession) -> int:
        result = await session.execute(select(func.count(User.id)))
        return int(result.scalar_one())

    @staticmethod
    async def iter_all_ids(session: AsyncSession, *, exclude_banned: bool = True):
        stmt = select(User.id)
        if exclude_banned:
            stmt = stmt.where(User.is_banned.is_(False))
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]


class JobRepository:
    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        job_id: str,
        user_id: int,
        job_type: str,
        platform: str | None = None,
        source_url: str | None = None,
    ) -> Job:
        job = Job(
            id=job_id,
            user_id=user_id,
            job_type=job_type,
            status="pending",
            platform=platform,
            source_url=source_url,
        )
        session.add(job)
        await session.flush()
        return job

    @staticmethod
    async def mark_processing(session: AsyncSession, job_id: str) -> None:
        job = await session.get(Job, job_id)
        if job is not None:
            job.status = "processing"

    @staticmethod
    async def mark_completed(session: AsyncSession, job_id: str, result_meta: dict[str, Any] | None = None) -> None:
        job = await session.get(Job, job_id)
        if job is not None:
            job.status = "completed"
            job.result_meta = result_meta

    @staticmethod
    async def mark_failed(session: AsyncSession, job_id: str, error_message: str) -> None:
        job = await session.get(Job, job_id)
        if job is not None:
            job.status = "failed"
            job.error_message = error_message[:2000]

    @staticmethod
    async def get(session: AsyncSession, job_id: str) -> Job | None:
        return await session.get(Job, job_id)

    @staticmethod
    async def delete_older_than(session: AsyncSession, days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await session.execute(delete(Job).where(Job.created_at < cutoff))
        return result.rowcount or 0


@dataclass(frozen=True)
class UsageStats:
    total_users: int
    new_users_7d: int
    jobs_total: int
    jobs_completed: int
    jobs_failed: int
    jobs_by_type: dict[str, int]
    jobs_by_platform: dict[str, int]


class StatsRepository:
    @staticmethod
    async def compute(session: AsyncSession) -> UsageStats:
        total_users = int((await session.execute(select(func.count(User.id)))).scalar_one())

        week_ago = datetime.now(UTC) - timedelta(days=7)
        new_users_7d = int(
            (await session.execute(select(func.count(User.id)).where(User.created_at >= week_ago))).scalar_one()
        )

        jobs_total = int((await session.execute(select(func.count(Job.id)))).scalar_one())
        jobs_completed = int(
            (await session.execute(select(func.count(Job.id)).where(Job.status == "completed"))).scalar_one()
        )
        jobs_failed = int(
            (await session.execute(select(func.count(Job.id)).where(Job.status == "failed"))).scalar_one()
        )

        by_type_rows = (await session.execute(select(Job.job_type, func.count(Job.id)).group_by(Job.job_type))).all()
        by_platform_rows = (
            await session.execute(
                select(Job.platform, func.count(Job.id)).where(Job.platform.is_not(None)).group_by(Job.platform)
            )
        ).all()

        return UsageStats(
            total_users=total_users,
            new_users_7d=new_users_7d,
            jobs_total=jobs_total,
            jobs_completed=jobs_completed,
            jobs_failed=jobs_failed,
            jobs_by_type={k: v for k, v in by_type_rows},
            jobs_by_platform={k: v for k, v in by_platform_rows},
        )


DEFAULT_CAPTION_TEMPLATES: dict[str, str] = {
    "video": "🎬 {title}\n\n{source}\n\n🤖 @{bot_username}",
    "audio": "🎵 {title} — {artist}\n\n{source}\n\n🤖 @{bot_username}",
    "image": "🖼 {source}\n\n🤖 @{bot_username}",
}


class CaptionRepository:
    @staticmethod
    async def get_template(session: AsyncSession, media_type: str) -> str:
        row = await session.get(CaptionTemplate, media_type)
        if row is not None:
            return row.template
        return DEFAULT_CAPTION_TEMPLATES.get(media_type, "{source}")

    @staticmethod
    async def set_template(session: AsyncSession, media_type: str, template: str, updated_by: int) -> None:
        row = await session.get(CaptionTemplate, media_type)
        if row is not None:
            row.template = template
            row.updated_by = updated_by
        else:
            session.add(CaptionTemplate(media_type=media_type, template=template, updated_by=updated_by))

    @staticmethod
    async def list_buttons(session: AsyncSession, media_type: str) -> list[CaptionButton]:
        stmt = (
            select(CaptionButton)
            .where(CaptionButton.is_active.is_(True))
            .where((CaptionButton.media_type == media_type) | (CaptionButton.media_type.is_(None)))
            .order_by(CaptionButton.position)
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def list_all_buttons(session: AsyncSession) -> list[CaptionButton]:
        stmt = select(CaptionButton).order_by(CaptionButton.position)
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def add_button(
        session: AsyncSession, *, label: str, url: str, media_type: str | None, position: int
    ) -> CaptionButton:
        button = CaptionButton(label=label, url=url, media_type=media_type, position=position)
        session.add(button)
        await session.flush()
        return button

    @staticmethod
    async def remove_button(session: AsyncSession, button_id: int) -> None:
        await session.execute(delete(CaptionButton).where(CaptionButton.id == button_id))


class PlatformRepository:
    @staticmethod
    async def is_enabled(session: AsyncSession, platform: str) -> bool:
        row = await session.get(PlatformSetting, platform)
        return True if row is None else row.enabled

    @staticmethod
    async def set_enabled(session: AsyncSession, platform: str, enabled: bool) -> None:
        row = await session.get(PlatformSetting, platform)
        if row is not None:
            row.enabled = enabled
        else:
            session.add(PlatformSetting(platform=platform, enabled=enabled))

    @staticmethod
    async def list_all(session: AsyncSession) -> list[PlatformSetting]:
        return list((await session.execute(select(PlatformSetting))).scalars().all())

    @staticmethod
    async def get_max_file_size_mb(session: AsyncSession, platform: str) -> int | None:
        row = await session.get(PlatformSetting, platform)
        return row.max_file_size_mb if row else None

    @staticmethod
    async def set_max_file_size_mb(session: AsyncSession, platform: str, max_mb: int | None) -> None:
        row = await session.get(PlatformSetting, platform)
        if row is not None:
            row.max_file_size_mb = max_mb
        else:
            session.add(PlatformSetting(platform=platform, enabled=True, max_file_size_mb=max_mb))


class BotSettingRepository:
    @staticmethod
    async def get(session: AsyncSession, key: str, default: Any = None) -> Any:
        from app.db.models import BotSetting

        row = await session.get(BotSetting, key)
        return row.value if row is not None else default

    @staticmethod
    async def set(session: AsyncSession, key: str, value: Any, updated_by: int | None = None) -> None:
        from app.db.models import BotSetting

        row = await session.get(BotSetting, key)
        if row is not None:
            row.value = value
            row.updated_by = updated_by
        else:
            session.add(BotSetting(key=key, value=value, updated_by=updated_by))


class BroadcastRepository:
    @staticmethod
    async def create(session: AsyncSession, *, broadcast_id: str, admin_id: int, message_text: str) -> Broadcast:
        broadcast = Broadcast(id=broadcast_id, admin_id=admin_id, message_text=message_text, status="pending")
        session.add(broadcast)
        await session.flush()
        return broadcast

    @staticmethod
    async def get(session: AsyncSession, broadcast_id: str) -> Broadcast | None:
        return await session.get(Broadcast, broadcast_id)

    @staticmethod
    async def update_progress(
        session: AsyncSession,
        broadcast_id: str,
        *,
        total_users: int | None = None,
        sent_count: int | None = None,
        failed_count: int | None = None,
        status: str | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None:
            return
        if total_users is not None:
            broadcast.total_users = total_users
        if sent_count is not None:
            broadcast.sent_count = sent_count
        if failed_count is not None:
            broadcast.failed_count = failed_count
        if status is not None:
            broadcast.status = status
        if completed_at is not None:
            broadcast.completed_at = completed_at
