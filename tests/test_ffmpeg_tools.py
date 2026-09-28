"""Tests for the ffmpeg/ffprobe wrapper.

These run against the REAL ffmpeg/ffprobe binaries (not mocked) because media
processing correctness can't be meaningfully faked — we generate small
synthetic test clips with ffmpeg itself and then verify our wrapper's output
against them. Requires ffmpeg/ffprobe on PATH; skipped automatically if not
found (e.g. a minimal CI image without ffmpeg installed).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.media.errors import FFmpegError, MediaValidationError
from app.services.media.ffmpeg_tools import MediaTools

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available on PATH",
)


@pytest.fixture
def tools() -> MediaTools:
    return MediaTools(timeout_seconds=30)


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    """A real 3-second H.264+AAC mp4, generated with ffmpeg's test source
    filters (no external fixture files needed)."""
    path = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def audio_only_file(tmp_path: Path) -> Path:
    path = tmp_path / "audio_only.mp3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            "-c:a", "libmp3lame", str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture
def not_a_media_file(tmp_path: Path) -> Path:
    path = tmp_path / "not_media.mp4"
    path.write_bytes(b"this is definitely not a video file, just plain text bytes")
    return path


async def test_probe_real_video_with_audio(tools: MediaTools, sample_video: Path) -> None:
    probe = await tools.probe(sample_video)

    assert probe.has_video is True
    assert probe.has_audio is True
    assert 2.5 < probe.duration_seconds < 3.5
    assert probe.size_bytes > 0


async def test_probe_audio_only_file(tools: MediaTools, audio_only_file: Path) -> None:
    probe = await tools.probe(audio_only_file)

    assert probe.has_video is False
    assert probe.has_audio is True
    assert 1.5 < probe.duration_seconds < 2.5


async def test_probe_rejects_non_media_file(tools: MediaTools, not_a_media_file: Path) -> None:
    with pytest.raises(MediaValidationError):
        await tools.probe(not_a_media_file)


async def test_probe_rejects_missing_file(tools: MediaTools, tmp_path: Path) -> None:
    with pytest.raises(MediaValidationError, match="does not exist"):
        await tools.probe(tmp_path / "missing.mp4")


async def test_probe_rejects_empty_file(tools: MediaTools, tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(MediaValidationError, match="empty"):
        await tools.probe(empty)


async def test_extract_audio_full_track(tools: MediaTools, sample_video: Path, tmp_path: Path) -> None:
    dest_dir = tmp_path / "out"
    audio_path = await tools.extract_audio(sample_video, dest_dir)

    assert audio_path.exists()
    assert audio_path.suffix == ".mp3"

    probe = await tools.probe(audio_path)
    assert probe.has_audio is True
    assert probe.has_video is False
    assert 2.5 < probe.duration_seconds < 3.5


async def test_extract_recognition_clip_respects_duration(tools: MediaTools, sample_video: Path, tmp_path: Path) -> None:
    dest_dir = tmp_path / "out"
    clip_path = await tools.extract_recognition_clip(sample_video, dest_dir, clip_seconds=1)

    probe = await tools.probe(clip_path)
    # Requested 1s from a 3s source; allow ffmpeg encoder rounding slack.
    assert 0.5 < probe.duration_seconds <= 1.5


async def test_extract_audio_raises_on_video_with_no_audio_track(tools: MediaTools, tmp_path: Path) -> None:
    silent_video = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15",
            "-c:v", "libx264", str(silent_video),
        ],
        check=True,
    )
    with pytest.raises(FFmpegError):
        await tools.extract_audio(silent_video, tmp_path / "out")


async def test_ffmpeg_binary_not_found_raises_clear_error(sample_video: Path, tmp_path: Path) -> None:
    broken_tools = MediaTools(ffmpeg_binary="/nonexistent/ffmpeg-binary", timeout_seconds=5)
    with pytest.raises(FFmpegError, match="not found"):
        await broken_tools.extract_audio(sample_video, tmp_path / "out")


async def test_ffprobe_timeout_raises_ffmpeg_error(sample_video: Path) -> None:
    # A 0-second timeout guarantees asyncio.wait_for fires before ffprobe can
    # possibly finish, exercising the real timeout-and-kill code path.
    impatient_tools = MediaTools(timeout_seconds=0)
    with pytest.raises((FFmpegError, MediaValidationError)):
        await impatient_tools.probe(sample_video)
