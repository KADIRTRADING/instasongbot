"""Local-disk storage backend: no S3 bucket required, generates our own
HMAC-signed, time-limited download URL served by the same webhook aiohttp app
(see app/webhook_app.py's /files/{filename} route) or, in long-polling mode, a
small dedicated aiohttp file server (see app/file_server.py).

This exists so the bot is genuinely usable on a single bare AWS EC2 instance
with zero AWS S3 setup: `STORAGE_BACKEND=local` (the default) just works, as
long as `PUBLIC_BASE_URL` points at a reachable address for that instance. For
anything beyond one box, S3StorageBackend is the recommended path (see
ARCHITECTURE.md §9, §11).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from urllib.parse import urlencode

from app.logging_conf import get_logger
from app.services.storage.base import StoredFile
from app.services.storage.signing import sign

logger = get_logger(__name__)


class LocalStorageBackend:
    def __init__(self, *, storage_dir: str, public_base_url: str, signing_secret: str, default_ttl_seconds: int = 3600) -> None:
        if not public_base_url:
            raise ValueError(
                "PUBLIC_BASE_URL is required when STORAGE_BACKEND=local (it's the address "
                "users' browsers will use to download large files — see README)"
            )
        self._dir = Path(storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._public_base_url = public_base_url.rstrip("/")
        self._secret = signing_secret
        self._default_ttl = default_ttl_seconds

    async def upload(self, local_path: Path, *, filename: str, content_type: str = "") -> StoredFile:
        destination = self._dir / filename
        await asyncio.to_thread(shutil.copy2, local_path, destination)

        signature, expires_at = sign(filename, self._secret, self._default_ttl)
        query = urlencode({"exp": expires_at, "sig": signature})
        url = f"{self._public_base_url}/files/{filename}?{query}"

        return StoredFile(url=url, expires_in_seconds=self._default_ttl)

    async def delete(self, filename: str) -> None:
        path = self._dir / filename
        try:
            await asyncio.to_thread(path.unlink, True)  # missing_ok=True
        except OSError as exc:
            logger.warning("local_storage_delete_failed", filename=filename, error=str(exc))

    @property
    def storage_dir(self) -> Path:
        return self._dir
