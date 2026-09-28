"""Typed application configuration loaded from environment variables / .env.

Every tunable in this file has a sane default EXCEPT secrets (bot token, DB URL,
API keys), which are required and will raise a clear validation error on startup
if missing. This is deliberate: failing fast on missing config is much easier to
debug than a bot that starts and then mysteriously 500s on the first real request.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.constants import RecognitionProviderName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # --- Telegram ---
    BOT_TOKEN: str = Field(..., description="Token from @BotFather")
    BOT_USERNAME: str = Field(
        default="",
        description="Bot @username without the @, used in caption templates as {bot_username}",
    )
    ADMIN_IDS: str = Field(
        default="",
        description="Comma-separated Telegram numeric user IDs allowed into the admin menu",
    )
    USE_WEBHOOK: bool = Field(default=False)
    WEBHOOK_BASE_URL: str = Field(default="", description="Public HTTPS base URL, e.g. https://bot.example.com")
    WEBHOOK_PATH: str = Field(default="/webhook")
    WEBHOOK_SECRET: str = Field(default="", description="Sent/verified via X-Telegram-Bot-Api-Secret-Token")
    WEB_SERVER_HOST: str = Field(default="0.0.0.0")
    WEB_SERVER_PORT: int = Field(default=8080)
    # Point this at a self-hosted Local Bot API Server (github.com/tdlib/telegram-bot-api)
    # to raise the 20MB getFile download ceiling up to 2000MB. Leave blank for the
    # standard https://api.telegram.org.
    TELEGRAM_API_BASE_URL: str = Field(default="")

    # --- Database ---
    DATABASE_URL: str = Field(
        ...,
        description="postgresql+asyncpg://user:pass@host:5432/dbname",
    )
    DB_POOL_SIZE: int = Field(default=10)
    DB_ECHO: bool = Field(default=False)

    # --- Redis ---
    REDIS_URL: str = Field(default="redis://localhost:6379/0")

    # --- Music recognition ---
    RECOGNITION_PROVIDER: RecognitionProviderName = Field(default=RecognitionProviderName.AUDD)
    AUDD_API_TOKEN: str = Field(default="")
    ACRCLOUD_HOST: str = Field(default="")
    ACRCLOUD_ACCESS_KEY: str = Field(default="")
    ACRCLOUD_ACCESS_SECRET: str = Field(default="")
    RECOGNITION_CLIP_SECONDS: int = Field(default=15, ge=5, le=60)
    RECOGNITION_TIMEOUT_SECONDS: int = Field(default=20, ge=5, le=120)

    # --- Downloads / conversion ---
    MAX_TELEGRAM_FETCH_MB: int = Field(
        default=20, description="Bot API getFile ceiling; raise only if using a local Bot API server"
    )
    TELEGRAM_DIRECT_UPLOAD_MB: int = Field(
        default=50, description="Above this, files are uploaded to storage and sent as a link"
    )
    MAX_DOWNLOAD_MB: int = Field(default=500, description="Hard cap on anything the worker will fetch from the internet")
    DOWNLOAD_TIMEOUT_SECONDS: int = Field(default=180)
    FFMPEG_TIMEOUT_SECONDS: int = Field(default=180)
    FFMPEG_BINARY: str = Field(default="ffmpeg")
    FFPROBE_BINARY: str = Field(default="ffprobe")
    YTDLP_COOKIES_FILE: str = Field(default="", description="Optional cookies.txt for yt-dlp (operator-supplied)")
    INSTAGRAM_COOKIES_FILE: str = Field(default="", description="Optional cookies.txt scoped to Instagram")
    # Cold default for the automatic video-quality pick (best|720|480|audio).
    # Admins can override this live via the admin panel (stored in bot_settings
    # under "auto_video_quality"); this is only the first-boot fallback.
    AUTO_VIDEO_QUALITY: str = Field(
        default="best", description="Automatic download quality: best|720|480|audio"
    )

    # --- Music search (text query -> numbered results) ---
    SEARCH_PROVIDER: str = Field(default="itunes", description="Music search provider: 'itunes'")
    SEARCH_COUNTRY: str = Field(default="US", description="iTunes storefront country code for search results")
    SEARCH_RESULTS_PER_PAGE: int = Field(default=10, ge=1, le=10)
    SEARCH_MAX_RESULTS: int = Field(default=50, ge=10, le=200, description="Total results fetched per query (paginated client-side)")
    SEARCH_TIMEOUT_SECONDS: int = Field(default=15, ge=5, le=60)

    # --- Storage (temp files + S3 for large-file links) ---
    WORKDIR: str = Field(default="/data/tmp")
    TEMP_FILE_MAX_AGE_MINUTES: int = Field(default=60)
    STORAGE_BACKEND: str = Field(default="local", description="'local' or 's3'")
    # Used by the local storage backend to build download links, and by the
    # webhook entrypoint to register the webhook URL with Telegram. Independent
    # of USE_WEBHOOK: even in long-polling mode, the local storage backend's
    # tiny file server (app/file_server.py) needs a public address to hand
    # out download links for files too large for direct Telegram upload.
    PUBLIC_BASE_URL: str = Field(
        default="", description="Public HTTPS base URL for large-file download links, e.g. https://bot.example.com"
    )
    S3_BUCKET: str = Field(default="")
    S3_REGION: str = Field(default="us-east-1")
    S3_ENDPOINT_URL: str = Field(default="", description="Set for non-AWS S3-compatible providers; blank for AWS")
    S3_ACCESS_KEY_ID: str = Field(default="")
    S3_SECRET_ACCESS_KEY: str = Field(default="")
    PRESIGNED_URL_TTL_SECONDS: int = Field(default=3600, ge=60, le=604800)

    # --- Rate limiting / abuse prevention ---
    RATE_LIMIT_RECOGNIZE_PER_MINUTE: int = Field(default=5)
    RATE_LIMIT_DOWNLOAD_PER_MINUTE: int = Field(default=5)
    RATE_LIMIT_CONVERT_PER_MINUTE: int = Field(default=5)
    RATE_LIMIT_GLOBAL_BURST_PER_MINUTE: int = Field(default=12)

    # --- Data retention ---
    JOBS_RETENTION_DAYS: int = Field(default=30)

    # --- Logging ---
    LOG_LEVEL: str = Field(default="INFO")
    LOG_JSON: bool = Field(default=True)

    # --- i18n ---
    DEFAULT_LANGUAGE: str = Field(default="uz")

    @field_validator("ADMIN_IDS")
    @classmethod
    def _validate_admin_ids(cls, v: str) -> str:
        # Validate format eagerly so a typo'd ID is caught at startup, not on first use.
        for chunk in v.split(","):
            chunk = chunk.strip()
            if chunk and not chunk.lstrip("-").isdigit():
                raise ValueError(f"ADMIN_IDS must be comma-separated integers, got invalid chunk: {chunk!r}")
        return v

    @property
    def admin_ids(self) -> frozenset[int]:
        return frozenset(int(x.strip()) for x in self.ADMIN_IDS.split(",") if x.strip())

    @property
    def telegram_api_url_override(self) -> str | None:
        return self.TELEGRAM_API_BASE_URL or None


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Tests override via env vars before first call."""
    return Settings()
