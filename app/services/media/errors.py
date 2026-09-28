"""Error taxonomy for ffmpeg/ffprobe operations."""

from __future__ import annotations


class MediaValidationError(Exception):
    """The input file failed a pre-flight check (not a real media file, zero
    duration, exceeds a configured limit) before we even invoked ffmpeg."""


class FFmpegError(Exception):
    """ffmpeg/ffprobe ran but failed, timed out, or produced no usable output."""
