"""Tests for the yt-dlp wrapper's own logic: format-list building and error
classification. The actual `yt_dlp.YoutubeDL` extraction was already verified
live during development (TikTok/Facebook/YouTube — see ARCHITECTURE.md §8);
these tests mock at the `_extract_info`/DownloadError boundary so they run
offline and fast, while still exercising every line of our parsing code
against realistic yt-dlp info-dict shapes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yt_dlp

from app.constants import MediaType, Platform
from app.services.downloader.errors import (
    ContentNotFoundError,
    LoginRequiredError,
    PrivateContentError,
    UnsupportedURLError,
)
from app.services.downloader.ytdlp_client import YtDlpClient

TIKTOK_INFO = {
    "title": "Cool video",
    "uploader": "scout2015",
    "thumbnail": "https://example.com/thumb.jpg",
    "duration": 10.5,
    "webpage_url": "https://www.tiktok.com/@scout2015/video/123",
    "ext": "mp4",
    "width": 720,
    "height": 1280,
    "filesize_approx": 2_000_000,
    "formats": [
        {
            "format_id": "bytevc1_720p",
            "vcodec": "h264",
            "acodec": "aac",
            "ext": "mp4",
            "width": 720,
            "height": 1280,
            "filesize": 2_000_000,
        }
    ],
}

YOUTUBE_INFO = {
    "title": "A YouTube video",
    "uploader": "SomeChannel",
    "thumbnail": "https://example.com/yt_thumb.jpg",
    "duration": 212.0,
    "webpage_url": "https://www.youtube.com/watch?v=abc123",
    "ext": "mp4",
    "formats": [
        {"format_id": "18", "vcodec": "h264", "acodec": "aac", "ext": "mp4", "height": 360, "filesize": 5_000_000},
        {"format_id": "22", "vcodec": "h264", "acodec": "aac", "ext": "mp4", "height": 720, "filesize": 15_000_000},
        {"format_id": "140", "vcodec": "none", "acodec": "aac", "ext": "m4a", "abr": 128, "filesize": 3_000_000},
        {"format_id": "160", "vcodec": "vp9", "acodec": "none", "ext": "webm", "height": 144, "filesize": 500_000},
    ],
}


async def test_probe_tiktok_builds_video_format() -> None:
    client = YtDlpClient()
    with patch.object(client, "_extract_info", return_value=TIKTOK_INFO):
        result = await client.probe("https://www.tiktok.com/@scout2015/video/123", Platform.TIKTOK)

    assert result.platform == "tiktok"
    assert result.title == "Cool video"
    assert result.uploader == "scout2015"
    assert result.duration_seconds == 10.5
    assert len(result.formats) == 1
    assert result.formats[0].media_type == MediaType.VIDEO
    assert result.formats[0].filesize_bytes == 2_000_000


async def test_probe_youtube_builds_multiple_formats_sorted_best_first() -> None:
    client = YtDlpClient()
    with patch.object(client, "_extract_info", return_value=YOUTUBE_INFO):
        result = await client.probe("https://www.youtube.com/watch?v=abc123", Platform.YOUTUBE)

    video_formats = [f for f in result.formats if f.media_type == MediaType.VIDEO]
    audio_formats = [f for f in result.formats if f.media_type == MediaType.AUDIO]

    assert len(video_formats) == 3  # 360p, 720p, and the video-only 144p webm
    assert len(audio_formats) == 1
    # Highest resolution video should come first.
    assert video_formats[0].height == 720
    assert video_formats[-1].height == 144
    # Audio-only format is labeled clearly.
    assert "Audio only" in audio_formats[0].label
    assert audio_formats[0].label == "Audio only (128kbps)"


async def test_probe_deduplicates_formats_with_same_label() -> None:
    info = {
        **TIKTOK_INFO,
        "formats": [
            {"format_id": "a", "vcodec": "h264", "acodec": "aac", "ext": "mp4", "height": 720, "filesize": 1_000_000},
            {"format_id": "b", "vcodec": "h264", "acodec": "aac", "ext": "mp4", "height": 720, "filesize": 1_100_000},
        ],
    }
    client = YtDlpClient()
    with patch.object(client, "_extract_info", return_value=info):
        result = await client.probe("https://www.tiktok.com/@x/video/1", Platform.TIKTOK)

    # Both formats render as "720p" — only the first should be kept.
    assert len(result.formats) == 1
    assert result.formats[0].format_id == "a"


async def test_probe_playlist_takes_first_entry() -> None:
    """The playlist-unwrapping logic lives inside `_extract_info` itself, so
    this test mocks one level deeper (yt_dlp.YoutubeDL) to actually exercise
    that unwrap code path rather than bypassing it.
    """
    client = YtDlpClient()
    playlist_info = {"_type": "playlist", "entries": [TIKTOK_INFO]}

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return playlist_info

    with patch("yt_dlp.YoutubeDL", FakeYDL):
        info = client._extract_info("https://www.tiktok.com/@x/video/1", Platform.TIKTOK)
    assert info["title"] == "Cool video"


@pytest.mark.parametrize(
    "message,expected_exc",
    [
        # "login required" (Instagram's combined anonymous-block message) is
        # DELIBERATELY LoginRequiredError now, NOT PrivateContentError — a
        # public reel blocked anonymously is not "private" (the fixed bug).
        ("ERROR: [Instagram] abc: Requested content is not available, rate-limit reached or login required", LoginRequiredError),
        # A genuinely-private marker still maps to PrivateContentError.
        ("ERROR: [Instagram] abc: This account is private", PrivateContentError),
        ("ERROR: [twitter] 123: No video could be found in this tweet", ContentNotFoundError),
        ("ERROR: [generic] Unsupported URL: https://example.com/x", UnsupportedURLError),
        ("ERROR: some totally unexpected failure", Exception),
    ],
)
def test_classify_error(message: str, expected_exc: type[Exception]) -> None:
    from app.services.downloader.errors import DownloadFailedError

    result = YtDlpClient()._classify_error(message)
    if expected_exc is Exception:
        assert isinstance(result, DownloadFailedError)
    else:
        assert isinstance(result, expected_exc)


async def test_probe_raises_content_not_found_when_extractor_reports_it() -> None:
    client = YtDlpClient()

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            raise yt_dlp.utils.DownloadError("ERROR: [twitter] 123: No video could be found in this tweet")

    with patch("yt_dlp.YoutubeDL", FakeYDL):
        with pytest.raises(ContentNotFoundError):
            await client.probe("https://twitter.com/user/status/123", Platform.TWITTER)


async def test_download_classifies_media_type_from_codecs_not_format_id_string(tmp_path: Path) -> None:
    """Regression test: a real yt-dlp audio-only format_id is often just a
    plain number (e.g. "140" for a YouTube m4a stream) with no "audio"
    substring anywhere in it. media_type must be derived from the actual
    downloaded stream's vcodec, not a string heuristic on format_id.
    """
    client = YtDlpClient()
    output_file = tmp_path / "video123.m4a"

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            output_file.write_bytes(b"fake-audio-data")
            return {
                "title": "Some Song",
                "uploader": "Some Channel",
                "requested_downloads": [{"filepath": str(output_file), "vcodec": "none", "acodec": "aac"}],
            }

    with patch("yt_dlp.YoutubeDL", FakeYDL):
        result = await client.download(
            "https://www.youtube.com/watch?v=abc123", Platform.YOUTUBE, "140", tmp_path, max_bytes=10_000_000
        )

    assert result.media_type == MediaType.AUDIO


async def test_download_classifies_video_format_correctly(tmp_path: Path) -> None:
    client = YtDlpClient()
    output_file = tmp_path / "video123.mp4"

    class FakeYDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            output_file.write_bytes(b"fake-video-data")
            return {
                "title": "Some Video",
                "uploader": "Some Channel",
                "requested_downloads": [{"filepath": str(output_file), "vcodec": "h264", "acodec": "aac"}],
            }

    with patch("yt_dlp.YoutubeDL", FakeYDL):
        result = await client.download(
            "https://www.youtube.com/watch?v=abc123", Platform.YOUTUBE, "22", tmp_path, max_bytes=10_000_000
        )

    assert result.media_type == MediaType.VIDEO


async def test_download_uses_max_filesize_and_reports_too_large(tmp_path: Path) -> None:
    from app.services.downloader.errors import FileTooLargeError

    client = YtDlpClient()

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError(
                "ERROR: File is larger than max-filesize, does not pass filesize filter"
            )

    with patch("yt_dlp.YoutubeDL", FakeYDL):
        with pytest.raises(FileTooLargeError):
            await client.download(
                "https://www.tiktok.com/@x/video/1", Platform.TIKTOK, "best", tmp_path, max_bytes=1024
            )
