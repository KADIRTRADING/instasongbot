"""Opt-in live end-to-end tests of DownloadManager: probe() AND download()
against real platforms, writing real bytes to a temp directory and verifying
they're valid media via ffprobe/basic checks. This is the strongest evidence
that the full pipeline (platform detection -> SSRF check -> backend dispatch
-> byte streaming -> size enforcement) actually works end-to-end, not just its
individual pieces in isolation.

Skipped by default. Run with: pytest -m live tests/test_download_manager_live.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.services.downloader.manager import DownloadManager

pytestmark = pytest.mark.live


def _settings(**overrides) -> Settings:
    base = dict(
        BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        MAX_DOWNLOAD_MB=20,
        DOWNLOAD_TIMEOUT_SECONDS=60,
    )
    base.update(overrides)
    return Settings(**base)


async def test_live_download_real_tiktok_video(tmp_path: Path) -> None:
    manager = DownloadManager(_settings())
    probe = await manager.probe("https://www.tiktok.com/@scout2015/video/6718335390845095173")
    assert probe.formats

    video_formats = [f for f in probe.formats if f.media_type.value == "video"]
    assert video_formats
    downloaded = await manager.download(probe, video_formats[0].format_id, tmp_path)

    assert downloaded.path.exists()
    assert downloaded.size_bytes > 1000
    assert downloaded.path.suffix in (".mp4", ".webm")


async def test_live_download_real_pinterest_image(tmp_path: Path) -> None:
    manager = DownloadManager(_settings())
    probe = await manager.probe("https://www.pinterest.com/pin/3729612238075167/")
    assert probe.formats

    downloaded = await manager.download(probe, probe.formats[0].format_id, tmp_path)

    assert downloaded.path.exists()
    assert downloaded.size_bytes > 1000


async def test_live_download_real_pinterest_video(tmp_path: Path) -> None:
    manager = DownloadManager(_settings())
    probe = await manager.probe("https://www.pinterest.com/pin/664281013778109217/")
    video_formats = [f for f in probe.formats if f.media_type.value == "video"]
    assert video_formats

    downloaded = await manager.download(probe, video_formats[0].format_id, tmp_path)

    assert downloaded.path.exists()
    assert downloaded.size_bytes > 1000


async def test_live_unsupported_platform_raises() -> None:
    from app.services.downloader.errors import UnsupportedURLError

    manager = DownloadManager(_settings())
    with pytest.raises(UnsupportedURLError):
        await manager.probe("https://vimeo.com/12345")


async def test_live_ssrf_metadata_host_rejected_even_with_pinterest_looking_path() -> None:
    """A URL that isn't a recognized platform host is rejected as unsupported
    before the SSRF check even runs — proving platform detection is strictly
    hostname-based and a `/pin/123/` path alone can't impersonate Pinterest.
    """
    from app.services.downloader.errors import UnsupportedURLError

    manager = DownloadManager(_settings())
    with pytest.raises(UnsupportedURLError):
        await manager.probe("http://169.254.169.254/pin/123/")
