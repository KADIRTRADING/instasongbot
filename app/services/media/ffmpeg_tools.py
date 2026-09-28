"""ffmpeg/ffprobe wrapper: media probing, audio extraction, and short-clip
trimming for the recognition flow. All subprocess calls go through
`asyncio.create_subprocess_exec` (never `shell=True`, arguments are always a
list) and are wrapped in `asyncio.wait_for` so a stuck ffmpeg process can never
hang a worker indefinitely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.services.media.errors import FFmpegError, MediaValidationError

# A generous but finite ceiling on how long an input can be before we bother
# probing/transcoding it at all — guards against a maliciously crafted "video"
# file that's technically valid but absurdly long, wasting worker time.
_MAX_PROBE_DURATION_SECONDS = 4 * 60 * 60  # 4 hours


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    has_video: bool
    has_audio: bool
    format_name: str
    size_bytes: int


class MediaTools:
    def __init__(self, ffmpeg_binary: str = "ffmpeg", ffprobe_binary: str = "ffprobe", timeout_seconds: int = 180) -> None:
        self._ffmpeg = ffmpeg_binary
        self._ffprobe = ffprobe_binary
        self._timeout = timeout_seconds

    async def probe(self, path: Path) -> MediaProbe:
        if not path.exists() or path.stat().st_size == 0:
            raise MediaValidationError(f"File does not exist or is empty: {path}")

        cmd = [
            self._ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        returncode, stdout, stderr = await self._run(cmd)
        if returncode != 0:
            raise MediaValidationError(f"ffprobe could not read this file (not a valid media file?): {stderr[:300]}")

        try:
            payload = json.loads(stdout)
        except ValueError as exc:
            raise MediaValidationError("ffprobe returned unparseable output") from exc

        fmt = payload.get("format") or {}
        streams = payload.get("streams") or []

        try:
            duration = float(fmt.get("duration", 0.0))
        except (TypeError, ValueError):
            duration = 0.0

        if duration <= 0:
            raise MediaValidationError("Media file has zero or unknown duration")
        if duration > _MAX_PROBE_DURATION_SECONDS:
            raise MediaValidationError(
                f"Media is too long ({duration / 3600:.1f}h), exceeds the {_MAX_PROBE_DURATION_SECONDS / 3600:.0f}h limit"
            )

        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        if not has_video and not has_audio:
            raise MediaValidationError("File has no video or audio stream")

        return MediaProbe(
            duration_seconds=duration,
            has_video=has_video,
            has_audio=has_audio,
            format_name=fmt.get("format_name", ""),
            size_bytes=int(fmt.get("size", path.stat().st_size)),
        )

    async def extract_audio(
        self, source: Path, destination_dir: Path, *, ext: str = "mp3", start_seconds: float = 0.0, duration_seconds: float | None = None
    ) -> Path:
        """Extract the audio track as a standalone file. If `duration_seconds`
        is given, only that many seconds starting at `start_seconds` are kept
        (used to build the short clip sent to the recognition provider);
        otherwise the full audio track is extracted (used for "convert to
        MP3").
        """
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{uuid4().hex}.{ext}"

        codec_args = ["-acodec", "libmp3lame", "-q:a", "2"] if ext == "mp3" else ["-acodec", "aac"]

        cmd = [self._ffmpeg, "-y", "-loglevel", "error"]
        if start_seconds > 0:
            cmd += ["-ss", str(start_seconds)]
        cmd += ["-i", str(source)]
        if duration_seconds is not None:
            cmd += ["-t", str(duration_seconds)]
        cmd += ["-vn", *codec_args, str(destination)]

        returncode, _, stderr = await self._run(cmd)
        if returncode != 0 or not destination.exists():
            destination.unlink(missing_ok=True)
            raise FFmpegError(f"Audio extraction failed: {stderr[:500]}")
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise FFmpegError("Audio extraction produced an empty file (source may have no audio track)")

        return destination

    async def extract_recognition_clip(self, source: Path, destination_dir: Path, *, clip_seconds: int) -> Path:
        """Convenience wrapper: a short mp3 clip from the start of the file,
        sized for the recognition provider's upload limits (see
        RECOGNITION_CLIP_SECONDS in config.py).
        """
        return await self.extract_audio(source, destination_dir, ext="mp3", start_seconds=0.0, duration_seconds=clip_seconds)

    async def _run(self, cmd: list[str]) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError as exc:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise FFmpegError(f"ffmpeg/ffprobe timed out after {self._timeout}s: {' '.join(cmd[:2])}") from exc
        except FileNotFoundError as exc:
            raise FFmpegError(f"Binary not found: {cmd[0]} (is ffmpeg installed in this image?)") from exc

        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_bytes.decode(errors="replace"),
            stderr_bytes.decode(errors="replace"),
        )
