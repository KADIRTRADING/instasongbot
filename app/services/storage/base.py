"""Storage backend interface: where a downloaded/converted file goes when it's
too large for Telegram's direct-upload ceiling (see ARCHITECTURE.md §9).

Two implementations:
  - LocalStorageBackend: serves files from disk via a lightweight signed-URL
    scheme of our own (useful for single-box deployments with no S3 bucket,
    e.g. "just get it running on one AWS EC2 instance" — see README).
  - S3StorageBackend: uploads to S3/S3-compatible storage and returns a real
    presigned URL. Recommended for production/AWS deployments.

Both return the same `StoredFile` shape so callers (the worker's delivery
code) never need to know which backend is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class StoredFile:
    url: str
    expires_in_seconds: int


class StorageBackend(Protocol):
    async def upload(self, local_path: Path, *, filename: str, content_type: str) -> StoredFile:
        """Store `local_path`'s contents and return a time-limited download URL."""
        ...

    async def delete(self, filename: str) -> None:
        """Best-effort removal of a previously uploaded file (data retention,
        see ARCHITECTURE.md §12). Never raises — a failed cleanup is logged,
        not fatal.
        """
        ...
