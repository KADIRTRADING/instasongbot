"""DownloadManager: the single entry point handlers/workers use for both the
"probe" step (show available options) and the "download" step (fetch bytes).

It dispatches to the Pinterest client or the yt-dlp client based on the
detected platform, and owns the one HTTP-streaming-with-size-cap code path
used for Pinterest's direct CDN URLs (yt-dlp enforces its own cap internally
via `max_filesize`, see ytdlp_client.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import Settings
from app.constants import MediaType, Platform
from app.services.downloader.errors import (
    DownloadFailedError,
    DownloadTimeoutError,
    FileTooLargeError,
    UnsupportedURLError,
)
from app.services.downloader.models import DownloadedFile, ProbeResult
from app.services.downloader.pinterest_client import PinterestClient
from app.services.downloader.url_utils import assert_public_http_url, detect_platform
from app.services.downloader.ytdlp_client import YtDlpClient

_STREAM_CHUNK_SIZE = 1024 * 256


class DownloadManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pinterest = PinterestClient(timeout_seconds=settings.DOWNLOAD_TIMEOUT_SECONDS)
        self._ytdlp = YtDlpClient(
            cookies_file=settings.YTDLP_COOKIES_FILE,
            instagram_cookies_file=settings.INSTAGRAM_COOKIES_FILE,
            timeout_seconds=settings.DOWNLOAD_TIMEOUT_SECONDS,
        )

    async def probe(self, url: str) -> ProbeResult:
        platform = detect_platform(url)
        if platform is None:
            raise UnsupportedURLError(f"No supported platform recognized for this link: {url}")

        assert_public_http_url(url)

        if platform == Platform.PINTEREST:
            return await self._pinterest.probe(url)
        return await self._ytdlp.probe(url, platform)

    async def download(self, probe: ProbeResult, format_id: str, destination_dir: Path) -> DownloadedFile:
        destination_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = self._settings.MAX_DOWNLOAD_MB * 1024 * 1024
        platform = Platform(probe.platform) if probe.platform in {p.value for p in Platform} else None

        if platform == Platform.PINTEREST:
            media_url, media_format = self._pinterest.resolve_format_url(probe, format_id)
            assert_public_http_url(media_url)
            if media_url.endswith(".m3u8"):
                return await self._download_hls(media_url, destination_dir, max_bytes, media_format.media_type)
            return await self._stream_to_disk(
                media_url, destination_dir, max_bytes, media_format.media_type, title=probe.title
            )

        return await self._ytdlp.download(
            probe.source_url, platform, format_id, destination_dir, max_bytes  # type: ignore[arg-type]
        )

    async def _stream_to_disk(
        self, url: str, destination_dir: Path, max_bytes: int, media_type: MediaType, *, title: str | None
    ) -> DownloadedFile:
        ext = url.split("?")[0].rsplit(".", 1)[-1][:8] or "bin"
        destination = destination_dir / f"{uuid4().hex}.{ext}"

        try:
            async with httpx.AsyncClient(timeout=self._settings.DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise DownloadFailedError(f"Upstream returned HTTP {response.status_code} for media URL")

                    content_length = response.headers.get("content-length")
                    if content_length and int(content_length) > max_bytes:
                        raise FileTooLargeError(
                            size_mb=int(content_length) / (1024 * 1024), limit_mb=max_bytes // (1024 * 1024)
                        )

                    written = 0
                    with destination.open("wb") as fh:
                        async for chunk in response.aiter_bytes(_STREAM_CHUNK_SIZE):
                            written += len(chunk)
                            if written > max_bytes:
                                fh.close()
                                destination.unlink(missing_ok=True)
                                raise FileTooLargeError(
                                    size_mb=written / (1024 * 1024), limit_mb=max_bytes // (1024 * 1024)
                                )
                            fh.write(chunk)
        except httpx.TimeoutException as exc:
            destination.unlink(missing_ok=True)
            raise DownloadTimeoutError(f"Timed out downloading media: {exc}") from exc
        except httpx.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise DownloadFailedError(f"Failed to download media: {exc}") from exc

        return DownloadedFile(
            path=destination,
            media_type=media_type,
            title=title,
            uploader=None,
            ext=ext,
            size_bytes=destination.stat().st_size,
        )

    async def _download_hls(
        self, m3u8_url: str, destination_dir: Path, max_bytes: int, media_type: MediaType
    ) -> DownloadedFile:
        """Remux an HLS (.m3u8) stream into a single MP4 via ffmpeg, used as the
        Pinterest video fallback when no progressive MP4 format is available.
        """
        destination = destination_dir / f"{uuid4().hex}.mp4"
        cmd = [
            self._settings.FFMPEG_BINARY,
            "-y",
            "-loglevel",
            "error",
            "-i",
            m3u8_url,
            "-c",
            "copy",
            "-bsf:a",
            "aac_adtstoasc",
            str(destination),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._settings.FFMPEG_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            destination.unlink(missing_ok=True)
            raise DownloadTimeoutError("Timed out remuxing HLS stream") from exc

        if proc.returncode != 0 or not destination.exists():
            destination.unlink(missing_ok=True)
            raise DownloadFailedError(f"ffmpeg failed to remux HLS stream: {stderr.decode(errors='replace')[:500]}")

        size_bytes = destination.stat().st_size
        if size_bytes > max_bytes:
            destination.unlink(missing_ok=True)
            raise FileTooLargeError(size_mb=size_bytes / (1024 * 1024), limit_mb=max_bytes // (1024 * 1024))

        return DownloadedFile(
            path=destination, media_type=media_type, title=None, uploader=None, ext="mp4", size_bytes=size_bytes
        )
