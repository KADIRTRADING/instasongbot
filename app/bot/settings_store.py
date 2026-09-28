"""Typed accessors for the admin-editable runtime settings stored in the
`bot_settings` KV table (BotSettingRepository).

Centralizes the two settings the automatic flow adds so the key strings,
defaults, and coercion live in exactly one place rather than being duplicated
across the worker (reads them to decide download quality / whether to attach
result buttons) and the admin panel (writes them). The env-sourced
`Settings.AUTO_VIDEO_QUALITY` is only the cold first-boot default; once an
admin sets a value it wins (same pattern as the existing max_download_mb
override — see app/workers/tasks.py's _resolve_max_download_bytes).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import BotSettingRepository
from app.services.downloader.quality import VALID_QUALITIES

KEY_AUTO_VIDEO_QUALITY = "auto_video_quality"
KEY_SHOW_RESULT_BUTTONS = "show_result_buttons"


async def get_auto_video_quality(session: AsyncSession, *, env_default: str) -> str:
    """Admin override wins; else the env cold default; else "best". Always
    returns a member of VALID_QUALITIES."""
    value = await BotSettingRepository.get(session, KEY_AUTO_VIDEO_QUALITY, default=None)
    if value in VALID_QUALITIES:
        return value
    if env_default in VALID_QUALITIES:
        return env_default
    return "best"


async def set_auto_video_quality(session: AsyncSession, quality: str, *, updated_by: int | None = None) -> None:
    if quality not in VALID_QUALITIES:
        raise ValueError(f"invalid auto_video_quality: {quality!r}")
    await BotSettingRepository.set(session, KEY_AUTO_VIDEO_QUALITY, quality, updated_by=updated_by)


async def get_show_result_buttons(session: AsyncSession, *, default: bool = True) -> bool:
    """Whether the optional "Find song / Extract MP3 / Other options" buttons
    are attached under auto-downloaded videos. Defaults to on."""
    value = await BotSettingRepository.get(session, KEY_SHOW_RESULT_BUTTONS, default=None)
    if value is None:
        return default
    return bool(value)


async def set_show_result_buttons(session: AsyncSession, enabled: bool, *, updated_by: int | None = None) -> None:
    await BotSettingRepository.set(session, KEY_SHOW_RESULT_BUTTONS, bool(enabled), updated_by=updated_by)
