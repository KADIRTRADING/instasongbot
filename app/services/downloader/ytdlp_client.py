"""yt-dlp wrapper for YouTube, TikTok, Instagram, Facebook, and X/Twitter.

yt-dlp is synchronous and CPU/IO-blocking under the hood (its own networking,
not asyncio), so every call here runs inside `asyncio.to_thread` to avoid
stalling the worker's event loop while a probe or download is in flight.

See ARCHITECTURE.md §8 for the honest per-platform support matrix — this
module does not special-case Instagram/X to hide their current upstream
extraction problems; it surfaces yt-dlp's real error message.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yt_dlp

from app.constants import MediaType, Platform
from app.services.downloader.errors import (
    ContentNotFoundError,
    DownloadFailedError,
    DownloadTimeoutError,
    FileTooLargeError,
    PrivateContentError,
    UnsupportedURLError,
)
from app.services.downloader.models import DownloadedFile, MediaFormat, ProbeResult

# Substrings from yt-dlp's own ExtractorError messages that reliably indicate
# a specific failure category across the platforms we support. yt-dlp doesn't
# expose a structured error code, so pattern-matching its (stable, documented)
# English messages is the accepted approach other projects use too.
_PRIVATE_MARKERS = (
    "private",
    "login required",
    "requires authentication",
    "rate-limit reached",
    "not authorized",
)
_NOT_FOUND_MARKERS = (
    "unavailable",
    "not found",
    "has been removed",
    "video unavailable",
    "no video could be found",
    "no video formats found",
    "empty media response",
)


class YtDlpClient:
    def __init__(self, cookies_file: str = "", instagram_cookies_file: str = "", timeout_seconds: int = 60) -> None:
        self._cookies_file = cookies_file or None
        self._instagram_cookies_file = instagram_cookies_file or None
        self._timeout = timeout_seconds

    def _base_opts(self, platform: Platform) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": self._timeout,
            "extractor_retries": 2,
        }
        # Instagram gets its own cookie file if configured (a session scoped
        # narrowly to Instagram is lower-risk than reusing one global cookie
        # jar across every platform).
        if platform == Platform.INSTAGRAM and self._instagram_cookies_file:
            opts["cookiefile"] = self._instagram_cookies_file
        elif self._cookies_file:
            opts["cookiefile"] = self._cookies_file
        return opts

    async def probe(self, url: str, platform: Platform) -> ProbeResult:
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(self._extract_info, url, platform), timeout=self._timeout
            )
        except TimeoutError as exc:
            raise DownloadTimeoutError(f"Timed out probing {url}") from exc
        return self._build_probe_result(url, platform, info)

    def _extract_info(self, url: str, platform: Platform) -> dict[str, Any]:
        opts = {**self._base_opts(platform), "skip_download": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            raise self._classify_error(str(exc)) from exc
        if info is None:
            raise ContentNotFoundError("No media info returned for this URL")
        # Playlists/multi-entry results: take the first real entry, since the
        # bot operates on a single piece of content per link.
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise ContentNotFoundError("This link has no downloadable entries")
            info = entries[0]
        return info

    @staticmethod
    def _classify_error(message: str) -> Exception:
        lowered = message.lower()
        if any(marker in lowered for marker in _PRIVATE_MARKERS):
            return PrivateContentError(message)
        if any(marker in lowered for marker in _NOT_FOUND_MARKERS):
            return ContentNotFoundError(message)
        if "unsupported url" in lowered:
            return UnsupportedURLError(message)
        return DownloadFailedError(message)

    def _build_probe_result(self, url: str, platform: Platform, info: dict[str, Any]) -> ProbeResult:
        formats: list[MediaFormat] = []
        raw_formats = info.get("formats") or []

        seen_labels: set[str] = set()
        for fmt in raw_formats:
            has_video = fmt.get("vcodec") not in (None, "none")
            has_audio = fmt.get("acodec") not in (None, "none")
            if not has_video and not has_audio:
                continue

            height = fmt.get("height")
            width = fmt.get("width")
            filesize = fmt.get("filesize") or fmt.get("filesize_approx")

            if has_video:
                # Conventional quality tiers (1080p/720p/480p...) name the
                # SHORT side. For portrait video (e.g. a 720x1280 TikTok clip)
                # that's the width, not the height — labeling it "1280p" would
                # be technically-derived-from-height but practically confusing,
                # since everyone calls that clip "720p".
                quality_px = min(height, width) if height and width else height
                label = f"{quality_px}p" if quality_px else fmt.get("format_note") or fmt.get("format_id", "video")
                media_type = MediaType.VIDEO
            else:
                abr = fmt.get("abr")
                label = f"Audio only ({int(abr)}kbps)" if abr else "Audio only"
                media_type = MediaType.AUDIO

            if label in seen_labels:
                continue
            seen_labels.add(label)

            formats.append(
                MediaFormat(
                    format_id=fmt["format_id"],
                    media_type=media_type,
                    label=label,
                    ext=fmt.get("ext", "mp4"),
                    filesize_bytes=int(filesize) if filesize else None,
                    width=fmt.get("width"),
                    height=height,
                )
            )

        # Always offer a plain "best available" video option and an
        # "extract audio" option even if yt-dlp didn't enumerate granular
        # per-resolution formats (some extractors, e.g. TikTok, return only
        # one combined format).
        if not any(f.media_type == MediaType.VIDEO for f in formats) and info.get("ext"):
            formats.insert(
                0,
                MediaFormat(
                    format_id="best",
                    media_type=MediaType.VIDEO,
                    label="Best available",
                    ext=info.get("ext", "mp4"),
                    filesize_bytes=info.get("filesize") or info.get("filesize_approx"),
                    width=info.get("width"),
                    height=info.get("height"),
                ),
            )

        # Sort video formats by descending resolution (short side, so portrait
        # and landscape videos both rank by their conventional quality tier)
        # so the "best" option is presented first in the keyboard.
        def _quality_key(f: MediaFormat) -> int:
            if f.width and f.height:
                return min(f.width, f.height)
            return f.height or f.width or 0

        formats.sort(key=lambda f: (f.media_type != MediaType.VIDEO, -_quality_key(f)))

        return ProbeResult(
            platform=platform.value,
            source_url=url,
            title=info.get("title"),
            uploader=info.get("uploader") or info.get("channel"),
            thumbnail_url=info.get("thumbnail"),
            duration_seconds=info.get("duration"),
            formats=tuple(formats),
            backend_payload={"webpage_url": info.get("webpage_url", url)},
        )

    async def download(
        self, url: str, platform: Platform, format_id: str, destination_dir: Path, max_bytes: int
    ) -> DownloadedFile:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._run_download, url, platform, format_id, destination_dir, max_bytes),
                timeout=self._timeout,
            )
        except TimeoutError as exc:
            raise DownloadTimeoutError(f"Timed out downloading {url}") from exc
        return result

    def _run_download(
        self, url: str, platform: Platform, format_id: str, destination_dir: Path, max_bytes: int
    ) -> DownloadedFile:
        format_selector = format_id if format_id != "best" else "best"

        opts = {
            **self._base_opts(platform),
            "format": format_selector,
            "outtmpl": str(destination_dir / "%(id)s.%(ext)s"),
            "max_filesize": max_bytes,
            "noprogress": True,
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            message = str(exc)
            if "max-filesize" in message.lower() or "does not pass filesize filter" in message.lower():
                raise FileTooLargeError(size_mb=0.0, limit_mb=max_bytes // (1024 * 1024)) from exc
            raise self._classify_error(message) from exc

        if info is None:
            raise DownloadFailedError("yt-dlp reported success but returned no info")
        if info.get("_type") == "playlist":
            info = (info.get("entries") or [None])[0]
            if info is None:
                raise DownloadFailedError("yt-dlp returned an empty playlist result")

        requested = (info.get("requested_downloads") or [{}])[0]
        final_path = Path(requested.get("filepath") or info.get("filepath"))
        if not final_path.exists():
            raise DownloadFailedError(f"Expected output file missing: {final_path}")

        size_bytes = final_path.stat().st_size
        if size_bytes > max_bytes:
            final_path.unlink(missing_ok=True)
            raise FileTooLargeError(size_mb=size_bytes / (1024 * 1024), limit_mb=max_bytes // (1024 * 1024))

        # Determine media_type from the ACTUAL downloaded stream's codecs
        # (reported by yt-dlp on the requested-download entry), never from
        # guessing at the format_id string — real yt-dlp audio-only format
        # IDs are often just numbers (e.g. "140"), which a substring check
        # for "audio" would misclassify as video.
        vcodec = requested.get("vcodec") or info.get("vcodec")
        media_type = MediaType.AUDIO if vcodec in (None, "none") else MediaType.VIDEO

        return DownloadedFile(
            path=final_path,
            media_type=media_type,
            title=info.get("title"),
            uploader=info.get("uploader") or info.get("channel"),
            ext=final_path.suffix.lstrip("."),
            size_bytes=size_bytes,
        )
