"""S3 / S3-compatible storage backend.

Uses `S3_ENDPOINT_URL` to support both real AWS S3 (leave it blank) and any
S3-compatible provider (MinIO, DigitalOcean Spaces, Cloudflare R2, Backblaze
B2, etc — set the endpoint). boto3's blocking calls are run in a thread via
`asyncio.to_thread` to avoid stalling the event loop.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.logging_conf import get_logger
from app.services.storage.base import StoredFile

logger = get_logger(__name__)


class S3StorageBackend:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        default_ttl_seconds: int = 3600,
        key_prefix: str = "instasongbot",
    ) -> None:
        if not bucket:
            raise ValueError("S3_BUCKET is required when STORAGE_BACKEND=s3")

        self._bucket = bucket
        self._default_ttl = default_ttl_seconds
        self._key_prefix = key_prefix.strip("/")

        session_kwargs: dict[str, str] = {}
        if access_key_id and secret_access_key:
            session_kwargs["aws_access_key_id"] = access_key_id
            session_kwargs["aws_secret_access_key"] = secret_access_key
        # If credentials are omitted, boto3 falls back to the standard chain
        # (environment, shared config, or an EC2/ECS instance role) — the
        # preferred path in AWS deployments: no long-lived keys in .env.

        client_kwargs: dict[str, object] = {
            "region_name": region,
            # SigV4 + path/virtual addressing works correctly against both AWS
            # and third-party S3-compatible endpoints (MinIO in particular
            # needs signature_version explicitly set to v4).
            "config": BotoConfig(signature_version="s3v4"),
        }
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._client = boto3.client("s3", **session_kwargs, **client_kwargs)

    def _object_key(self, filename: str) -> str:
        return f"{self._key_prefix}/{filename}" if self._key_prefix else filename

    async def upload(self, local_path: Path, *, filename: str, content_type: str = "") -> StoredFile:
        key = self._object_key(filename)
        resolved_content_type = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        try:
            await asyncio.to_thread(
                self._client.upload_file,
                str(local_path),
                self._bucket,
                key,
                ExtraArgs={"ContentType": resolved_content_type},
            )
            url = await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=self._default_ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error("s3_upload_failed", key=key, error=str(exc))
            raise

        return StoredFile(url=url, expires_in_seconds=self._default_ttl)

    async def delete(self, filename: str) -> None:
        key = self._object_key(filename)
        try:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            # Deletion is best-effort cleanup, not a user-facing operation —
            # log and move on rather than propagating (see base.py docstring).
            logger.warning("s3_delete_failed", key=key, error=str(exc))
