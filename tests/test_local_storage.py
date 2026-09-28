"""Tests for LocalStorageBackend: real disk writes, real signed URLs."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.storage.local_backend import LocalStorageBackend
from app.services.storage.signing import verify


@pytest.fixture
def backend(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(
        storage_dir=str(tmp_path / "storage"),
        public_base_url="https://bot.example.com",
        signing_secret="test-secret",
        default_ttl_seconds=3600,
    )


async def test_upload_copies_file_and_returns_signed_url(backend: LocalStorageBackend, tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"fake mp3 bytes")

    stored = await backend.upload(source, filename="output.mp3", content_type="audio/mpeg")

    assert stored.expires_in_seconds == 3600
    assert stored.url.startswith("https://bot.example.com/files/output.mp3?")
    assert "sig=" in stored.url
    assert "exp=" in stored.url

    copied_path = backend.storage_dir / "output.mp3"
    assert copied_path.exists()
    assert copied_path.read_bytes() == b"fake mp3 bytes"


async def test_upload_generates_url_with_valid_signature(backend: LocalStorageBackend, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fake video bytes")

    stored = await backend.upload(source, filename="clip.mp4", content_type="video/mp4")

    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(stored.url)
    query = parse_qs(parsed.query)
    signature = query["sig"][0]
    expires_at = int(query["exp"][0])

    assert verify("clip.mp4", expires_at, signature, secret="test-secret") is True


async def test_delete_removes_file(backend: LocalStorageBackend, tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"data")
    await backend.upload(source, filename="to_delete.mp3", content_type="audio/mpeg")

    assert (backend.storage_dir / "to_delete.mp3").exists()
    await backend.delete("to_delete.mp3")
    assert not (backend.storage_dir / "to_delete.mp3").exists()


async def test_delete_nonexistent_file_does_not_raise(backend: LocalStorageBackend) -> None:
    await backend.delete("never_existed.mp3")  # should not raise


def test_requires_public_base_url(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        LocalStorageBackend(storage_dir=str(tmp_path), public_base_url="", signing_secret="x")
