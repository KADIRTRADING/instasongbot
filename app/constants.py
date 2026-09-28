"""Shared enums and constants used across the app and worker processes."""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    UZ = "uz"
    RU = "ru"
    EN = "en"


DEFAULT_LANGUAGE = Language.UZ
SUPPORTED_LANGUAGES: tuple[Language, ...] = (Language.UZ, Language.RU, Language.EN)


class Platform(StrEnum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TWITTER = "twitter"
    PINTEREST = "pinterest"


class MediaType(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"


class JobType(StrEnum):
    RECOGNIZE = "recognize"
    PROBE = "probe"
    DOWNLOAD = "download"
    CONVERT = "convert"
    BROADCAST = "broadcast"


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class RateLimitAction(StrEnum):
    RECOGNIZE = "recognize"
    DOWNLOAD = "download"
    CONVERT = "convert"
    GLOBAL_BURST = "global_burst"


class RecognitionProviderName(StrEnum):
    AUDD = "audd"
    ACRCLOUD = "acrcloud"


# Telegram Bot API hard platform limits (see ARCHITECTURE.md §9).
TELEGRAM_BOT_API_UPLOAD_LIMIT_MB = 50
TELEGRAM_BOT_API_DOWNLOAD_LIMIT_MB = 20

CALLBACK_SEP = ":"
