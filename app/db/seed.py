"""Idempotent seed of default rows (caption templates, platform toggles).

Run automatically on bot startup (see app/main.py) — safe to run every time
since it only inserts rows that don't already exist yet.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import Platform
from app.db.models import CaptionTemplate, PlatformSetting
from app.db.repositories import DEFAULT_CAPTION_TEMPLATES
from app.logging_conf import get_logger

logger = get_logger(__name__)


async def seed_defaults(session: AsyncSession) -> None:
    for media_type, template in DEFAULT_CAPTION_TEMPLATES.items():
        existing = await session.get(CaptionTemplate, media_type)
        if existing is None:
            session.add(CaptionTemplate(media_type=media_type, template=template))
            logger.info("seed_caption_template", media_type=media_type)

    for platform in Platform:
        existing = await session.get(PlatformSetting, platform.value)
        if existing is None:
            session.add(PlatformSetting(platform=platform.value, enabled=True))
            logger.info("seed_platform_setting", platform=platform.value)

    await session.commit()
