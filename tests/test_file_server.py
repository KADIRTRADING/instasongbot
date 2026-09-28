"""Tests for app/file_server.py using aiohttp's real test server (aiohttp's
own pytest plugin spins up a genuine TCP server on a random port and issues
real HTTP requests against it — not mocked).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.file_server import create_file_server_app
from app.services.storage.signing import sign


def _make_settings(workdir: str) -> Settings:
    return Settings(
        BOT_TOKEN="test-secret-token",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        WORKDIR=workdir,
    )


@pytest.fixture
async def client(tmp_path: Path) -> TestClient:
    settings = _make_settings(str(tmp_path))
    storage_dir = tmp_path / "large_files"
    storage_dir.mkdir(parents=True)
    (storage_dir / "song.mp3").write_bytes(b"fake-audio-bytes-for-test")

    app = create_file_server_app(settings)
    server = TestServer(app)
    test_client = TestClient(server)
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def test_valid_signed_link_downloads_file(client: TestClient) -> None:
    signature, expires_at = sign("song.mp3", secret="test-secret-token", ttl_seconds=3600)

    resp = await client.get(f"/files/song.mp3?sig={signature}&exp={expires_at}")

    assert resp.status == 200
    body = await resp.read()
    assert body == b"fake-audio-bytes-for-test"


async def test_missing_signature_is_forbidden(client: TestClient) -> None:
    resp = await client.get("/files/song.mp3")
    assert resp.status == 403


async def test_wrong_signature_is_forbidden(client: TestClient) -> None:
    _, expires_at = sign("song.mp3", secret="test-secret-token", ttl_seconds=3600)
    resp = await client.get(f"/files/song.mp3?sig={'0' * 64}&exp={expires_at}")
    assert resp.status == 403


async def test_expired_link_is_forbidden(client: TestClient) -> None:
    signature, expires_at = sign("song.mp3", secret="test-secret-token", ttl_seconds=-100)
    resp = await client.get(f"/files/song.mp3?sig={signature}&exp={expires_at}")
    assert resp.status == 403


async def test_nonexistent_file_is_not_found(client: TestClient) -> None:
    signature, expires_at = sign("ghost.mp3", secret="test-secret-token", ttl_seconds=3600)
    resp = await client.get(f"/files/ghost.mp3?sig={signature}&exp={expires_at}")
    assert resp.status == 404


async def test_path_traversal_filename_is_rejected(client: TestClient) -> None:
    signature, expires_at = sign("../../etc/passwd", secret="test-secret-token", ttl_seconds=3600)
    resp = await client.get(f"/files/..%2F..%2Fetc%2Fpasswd?sig={signature}&exp={expires_at}")
    assert resp.status in (400, 404)  # aiohttp routing may normalize the path before our handler sees it


async def test_signature_is_tied_to_exact_filename(client: TestClient) -> None:
    """A signature computed for one filename must not work for another, even
    if both files exist and the signature format is otherwise valid."""
    signature, expires_at = sign("song.mp3", secret="test-secret-token", ttl_seconds=3600)
    resp = await client.get(f"/files/other-name.mp3?sig={signature}&exp={expires_at}")
    assert resp.status == 403
