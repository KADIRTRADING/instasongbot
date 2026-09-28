"""Pinterest client: talks directly to Pinterest's public, unauthenticated JSON
resource API — the same endpoint Pinterest's own logged-out web client and
yt-dlp/gallery-dl use. No login, no API key (Pinterest has no public download
API). Verified live during development (see ARCHITECTURE.md §7).

We only ever read what an anonymous browser could already see; private
boards/pins return an error from Pinterest itself and surface as
PrivateContentError/ContentNotFoundError here, never bypassed.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.constants import MediaType, Platform
from app.services.downloader.errors import (
    ContentNotFoundError,
    DownloadFailedError,
    PrivateContentError,
    RateLimitedError,
)
from app.services.downloader.models import MediaFormat, ProbeResult
from app.services.downloader.url_utils import assert_public_http_url

_PIN_ID_RE = re.compile(r"pinterest\.[a-z.]+/pin/(?:[\w-]+--)?(\d+)", re.IGNORECASE)
_PINIT_RE = re.compile(r"pin\.it/([^/?#]+)", re.IGNORECASE)

_RESOURCE_BASE = "https://www.pinterest.com/resource/PinResource/get/"
_COMMON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*, q=0.01",
    "X-Pinterest-PWS-Handler": "www/[username].js",
    "X-Requested-With": "XMLHttpRequest",
}

# Preferred video quality order — first match wins. V_720P is a direct,
# progressive MP4 (a plain streamed download); the HLS variants are kept as a
# last resort since they need ffmpeg to fetch+remux into a single file rather
# than a direct byte-for-byte download.
_VIDEO_FORMAT_PREFERENCE = ("V_720P", "V_HLSV4", "V_HLSV3_WEB", "V_HLSV3_MOBILE")


class PinterestClient:
    def __init__(self, timeout_seconds: int = 30) -> None:
        self._timeout = timeout_seconds

    async def probe(self, url: str) -> ProbeResult:
        pin_id = await self._resolve_pin_id(url)
        pin_data = await self._fetch_pin(pin_id)
        return self._build_probe_result(url, pin_data)

    async def _resolve_pin_id(self, url: str) -> str:
        match = _PIN_ID_RE.search(url)
        if match:
            return match.group(1)

        shortlink_match = _PINIT_RE.search(url)
        if shortlink_match:
            assert_public_http_url(url)
            async with httpx.AsyncClient(follow_redirects=True, timeout=self._timeout) as client:
                try:
                    response = await client.get(url, headers=_COMMON_HEADERS)
                except httpx.HTTPError as exc:
                    raise DownloadFailedError(f"Failed to resolve Pinterest short link: {exc}") from exc
            resolved_match = _PIN_ID_RE.search(str(response.url))
            if resolved_match:
                return resolved_match.group(1)
            raise ContentNotFoundError("Pinterest short link did not resolve to a pin")

        raise ContentNotFoundError("Could not find a Pinterest pin ID in this URL")

    async def _fetch_pin(self, pin_id: str) -> dict[str, Any]:
        options = {"id": pin_id, "field_set_key": "detailed"}
        params = {"data": json.dumps({"options": options})}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(_RESOURCE_BASE, params=params, headers=_COMMON_HEADERS)
        except httpx.TimeoutException as exc:
            raise DownloadFailedError(f"Pinterest request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise DownloadFailedError(f"Pinterest request failed: {exc}") from exc

        if response.status_code == 404:
            raise ContentNotFoundError("Pinterest pin not found (deleted or invalid ID)")
        if response.status_code == 429:
            raise RateLimitedError("Pinterest is rate-limiting requests right now")
        if response.status_code >= 500:
            raise DownloadFailedError(f"Pinterest returned HTTP {response.status_code}")
        if response.status_code != 200:
            raise DownloadFailedError(f"Pinterest returned unexpected HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise DownloadFailedError("Pinterest returned a non-JSON response") from exc

        resource_response = payload.get("resource_response") or {}
        if resource_response.get("status") != "success":
            code = resource_response.get("code")
            message = resource_response.get("message", "unknown error")
            if code == 401 or "private" in str(message).lower():
                raise PrivateContentError("This pin is private or requires login to view")
            raise ContentNotFoundError(f"Pinterest could not return this pin: {message}")

        data = resource_response.get("data")
        if not data:
            raise ContentNotFoundError("Pinterest returned an empty pin")
        return data

    def _build_probe_result(self, source_url: str, data: dict[str, Any]) -> ProbeResult:
        title = _clean_text(data.get("title") or data.get("grid_title") or data.get("seo_title"))
        uploader = None
        pinner = data.get("pinner") or {}
        if pinner.get("username"):
            uploader = pinner["username"]

        thumbnail_url = ((data.get("images") or {}).get("236x") or {}).get("url")

        formats: list[MediaFormat] = []

        story_pin_data = data.get("story_pin_data")
        carousel_data = data.get("carousel_data")
        videos = data.get("videos")

        if story_pin_data and story_pin_data.get("pages"):
            formats.extend(self._formats_from_story(story_pin_data))
        elif carousel_data and carousel_data.get("carousel_slots"):
            formats.extend(self._formats_from_carousel(carousel_data))
        elif videos and videos.get("video_list"):
            fmt = self._best_video_format(videos["video_list"])
            if fmt:
                formats.append(fmt)
        else:
            orig = (data.get("images") or {}).get("orig")
            if orig and orig.get("url"):
                formats.append(
                    MediaFormat(
                        format_id="image:0",
                        media_type=MediaType.IMAGE,
                        label="Image",
                        ext=_ext_from_url(orig["url"], default="jpg"),
                        filesize_bytes=None,
                        width=orig.get("width"),
                        height=orig.get("height"),
                    )
                )

        if not formats:
            raise ContentNotFoundError("No downloadable image or video found on this pin")

        duration_seconds = None
        if videos and videos.get("video_list"):
            any_fmt = next(iter(videos["video_list"].values()), {})
            if any_fmt.get("duration"):
                duration_seconds = float(any_fmt["duration"]) / 1000.0

        return ProbeResult(
            platform=Platform.PINTEREST.value,
            source_url=source_url,
            title=title,
            uploader=uploader,
            thumbnail_url=thumbnail_url,
            duration_seconds=duration_seconds,
            formats=tuple(formats),
            backend_payload={"pin_data": data},
        )

    @staticmethod
    def _best_video_format(video_list: dict[str, Any]) -> MediaFormat | None:
        for preferred in _VIDEO_FORMAT_PREFERENCE:
            if preferred in video_list:
                info = video_list[preferred]
                is_hls = "HLS" in preferred
                label = "Video (HLS)" if is_hls else f"Video {info.get('width', '?')}x{info.get('height', '?')}"
                return MediaFormat(
                    format_id=f"video:{preferred}",
                    media_type=MediaType.VIDEO,
                    label=label,
                    ext="mp4",  # HLS gets remuxed to mp4 by the manager, see manager.py
                    filesize_bytes=None,
                    width=info.get("width"),
                    height=info.get("height"),
                )
        # Fall back to the largest available format by width if none of our
        # preferred keys are present.
        if not video_list:
            return None
        best_key, best_info = max(video_list.items(), key=lambda kv: kv[1].get("width", 0))
        return MediaFormat(
            format_id=f"video:{best_key}",
            media_type=MediaType.VIDEO,
            label=f"Video {best_info.get('width', '?')}x{best_info.get('height', '?')}",
            ext="mp4",
            filesize_bytes=None,
            width=best_info.get("width"),
            height=best_info.get("height"),
        )

    @staticmethod
    def _formats_from_carousel(carousel_data: dict[str, Any]) -> list[MediaFormat]:
        formats: list[MediaFormat] = []
        slots = carousel_data.get("carousel_slots") or []
        for index, slot in enumerate(slots):
            images = slot.get("images") or {}
            if not images:
                continue
            size_key, image = next(iter(images.items()))
            url = image.get("url", "")
            # Carousel thumbnails are served at a fixed size (e.g. "1200x") —
            # swap the size segment for "originals" to get the full-resolution
            # image, matching what gallery-dl's Pinterest extractor does.
            full_url = url.replace(f"/{size_key}/", "/originals/", 1) if size_key in url else url
            formats.append(
                MediaFormat(
                    format_id=f"carousel:{index}",
                    media_type=MediaType.IMAGE,
                    label=f"Image {index + 1} of {len(slots)}",
                    ext=_ext_from_url(full_url, default="jpg"),
                    filesize_bytes=None,
                    width=image.get("width"),
                    height=image.get("height"),
                )
            )
        return formats

    @staticmethod
    def _formats_from_story(story_pin_data: dict[str, Any]) -> list[MediaFormat]:
        formats: list[MediaFormat] = []
        block_index = 0
        for page in story_pin_data.get("pages") or []:
            for block in page.get("blocks") or []:
                block_type = block.get("type")
                if block_type == "story_pin_image_block" or "image" in block:
                    formats.append(
                        MediaFormat(
                            format_id=f"story_image:{block_index}",
                            media_type=MediaType.IMAGE,
                            label=f"Image {block_index + 1}",
                            ext="jpg",
                        )
                    )
                elif block_type == "story_pin_video_block" or "video" in block:
                    formats.append(
                        MediaFormat(
                            format_id=f"story_video:{block_index}",
                            media_type=MediaType.VIDEO,
                            label=f"Video {block_index + 1}",
                            ext="mp4",
                        )
                    )
                block_index += 1
        return formats

    @staticmethod
    def resolve_format_url(probe: ProbeResult, format_id: str) -> tuple[str, MediaFormat]:
        """Resolve a previously-probed format_id back to a concrete, fetchable
        URL. DownloadManager calls this then streams the URL itself (shared
        HTTP-streaming/size-cap code path also used for plain direct-link
        fallbacks), so this client never duplicates that logic.
        """
        pin_data = probe.backend_payload.get("pin_data", {})
        media_format = next((f for f in probe.formats if f.format_id == format_id), None)
        if media_format is None:
            raise ContentNotFoundError(f"Unknown format_id for this pin: {format_id}")

        kind, _, index_or_key = format_id.partition(":")

        if kind == "image":
            url = ((pin_data.get("images") or {}).get("orig") or {}).get("url")
            if not url:
                raise ContentNotFoundError("Image URL missing from Pinterest response")
            return url, media_format

        if kind == "video":
            video_list = ((pin_data.get("videos") or {}).get("video_list")) or {}
            info = video_list.get(index_or_key)
            if not info or not info.get("url"):
                raise ContentNotFoundError("Video URL missing from Pinterest response")
            return info["url"], media_format

        if kind == "carousel":
            slots = ((pin_data.get("carousel_data") or {}).get("carousel_slots")) or []
            idx = int(index_or_key)
            if idx >= len(slots):
                raise ContentNotFoundError("Carousel image index out of range")
            images = slots[idx].get("images") or {}
            size_key, image = next(iter(images.items()))
            url = image.get("url", "")
            full_url = url.replace(f"/{size_key}/", "/originals/", 1) if size_key in url else url
            return full_url, media_format

        if kind in ("story_image", "story_video"):
            story = pin_data.get("story_pin_data") or {}
            target_index = int(index_or_key)
            counter = 0
            for page in story.get("pages") or []:
                for block in page.get("blocks") or []:
                    if counter == target_index:
                        if kind == "story_video":
                            video_list = (block.get("video") or {}).get("video_list") or {}
                            best = video_list.get("V_HLSV4") or next(iter(video_list.values()), {})
                            if best.get("url"):
                                return best["url"], media_format
                        else:
                            image_block = block.get("image") or {}
                            originals = (image_block.get("images") or {}).get("originals") or {}
                            if originals.get("url"):
                                return originals["url"], media_format
                    counter += 1
            raise ContentNotFoundError("Story block not found for this format")

        raise ContentNotFoundError(f"Unrecognized format_id shape: {format_id}")


def _clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    return text or None


def _ext_from_url(url: str, *, default: str) -> str:
    path = url.split("?")[0]
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1].lower()
    return default
