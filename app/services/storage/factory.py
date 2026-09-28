"""Selects the configured storage backend. One switch, not a code fork."""

from __future__ import annotations

from app.config import Settings
from app.services.storage.base import StorageBackend
from app.services.storage.local_backend import LocalStorageBackend
from app.services.storage.s3_backend import S3StorageBackend


def get_storage_backend(settings: Settings) -> StorageBackend:
    if settings.STORAGE_BACKEND == "s3":
        return S3StorageBackend(
            bucket=settings.S3_BUCKET,
            region=settings.S3_REGION,
            endpoint_url=settings.S3_ENDPOINT_URL,
            access_key_id=settings.S3_ACCESS_KEY_ID,
            secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            default_ttl_seconds=settings.PRESIGNED_URL_TTL_SECONDS,
        )
    if settings.STORAGE_BACKEND == "local":
        return LocalStorageBackend(
            storage_dir=f"{settings.WORKDIR}/large_files",
            public_base_url=settings.PUBLIC_BASE_URL or settings.WEBHOOK_BASE_URL,
            signing_secret=settings.BOT_TOKEN,  # already secret, already unique per deployment
            default_ttl_seconds=settings.PRESIGNED_URL_TTL_SECONDS,
        )
    raise ValueError(f"Unknown STORAGE_BACKEND: {settings.STORAGE_BACKEND!r}")
