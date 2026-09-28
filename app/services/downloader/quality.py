"""Automatic format selection for the no-menu download flow.

In the old menu-driven UX the user picked a format from a keyboard. The new
automatic flow (see the project brief / ARCHITECTURE §4.2) skips that: after
probing a link we auto-pick ONE format based on the admin's configured
`auto_video_quality` setting and download it directly, no button tap.

`pick_auto_format` is a pure function over a probed `ProbeResult` so it's
trivially unit-testable and identical whether called from the worker or a
handler. It returns the chosen `MediaFormat`, or None if the probe has no
usable formats at all (caller then surfaces a "nothing downloadable" error).

Quality vocabulary (stored in bot_settings["auto_video_quality"]):
  - "best"  -> highest-resolution video available (the default)
  - "720"   -> best video whose short side is <= 720p, else the smallest
               available video (never silently upgrade past the cap)
  - "480"   -> same rule at 480p
  - "audio" -> an audio-only format if one exists, else falls back to best
               video (so an audio-only preference never yields nothing)
Formats arrive already sorted best-first by the backends (see
ytdlp_client._build_probe_result), which this relies on for stable tie-breaks.
"""

from __future__ import annotations

from app.constants import MediaType
from app.services.downloader.models import MediaFormat, ProbeResult

VALID_QUALITIES = ("best", "720", "480", "audio")
_CAP_BY_QUALITY = {"720": 720, "480": 480}


def _short_side(fmt: MediaFormat) -> int | None:
    if fmt.width and fmt.height:
        return min(fmt.width, fmt.height)
    return fmt.height or fmt.width or None


def pick_auto_format(probe: ProbeResult, quality: str) -> MediaFormat | None:
    """Choose one format to auto-download for the given quality preference."""
    formats = list(probe.formats)
    if not formats:
        return None

    quality = quality if quality in VALID_QUALITIES else "best"
    videos = [f for f in formats if f.media_type == MediaType.VIDEO]
    audios = [f for f in formats if f.media_type == MediaType.AUDIO]

    if quality == "audio":
        if audios:
            return audios[0]
        # No audio-only stream (common — many extractors return combined
        # video only). Fall back to best video rather than nothing; the caller
        # can still extract MP3 from it via the "Extract MP3" action.
        return videos[0] if videos else formats[0]

    if quality in _CAP_BY_QUALITY and videos:
        cap = _CAP_BY_QUALITY[quality]
        # Formats are best-first; the first at-or-below the cap is the best
        # that respects it.
        for fmt in videos:
            side = _short_side(fmt)
            if side is None or side <= cap:
                return fmt
        # Every video exceeds the cap — pick the smallest (last, since sorted
        # descending) rather than exceed the operator's chosen ceiling.
        return videos[-1]

    # "best" (or a cap with no video formats): first video, else first format.
    return videos[0] if videos else formats[0]
