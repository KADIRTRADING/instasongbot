"""Shared data shapes produced by every downloader backend (yt-dlp wrapper and
the Pinterest client) so the rest of the app deals with one uniform shape
regardless of which backend resolved a given URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.constants import MediaType


@dataclass(frozen=True)
class MediaFormat:
    """One selectable download option, shown to the user as an inline button."""

    format_id: str
    media_type: MediaType
    label: str  # human-readable, e.g. "720p MP4" / "Audio only (MP3)" / "Image 2 of 5"
    ext: str
    filesize_bytes: int | None = None  # None when the platform doesn't report it upfront
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class ProbeResult:
    """Result of a metadata-only probe: what's downloadable at this URL, with
    no bytes fetched yet. Powers the inline keyboard of real, available options.
    """

    platform: str
    source_url: str
    title: str | None
    uploader: str | None
    thumbnail_url: str | None
    duration_seconds: float | None
    formats: tuple[MediaFormat, ...]
    # Opaque backend-specific payload (e.g. the full yt-dlp info dict, or the
    # parsed Pinterest pin JSON) needed to actually perform the download later
    # without re-probing. Never exposed to the user directly.
    backend_payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadedFile:
    """Result of actually fetching bytes to local disk."""

    path: Path
    media_type: MediaType
    title: str | None
    uploader: str | None
    ext: str
    size_bytes: int
