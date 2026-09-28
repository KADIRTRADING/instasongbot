"""Provider-agnostic music recognition interface.

Both AudD and ACRCloud implement `MusicRecognitionProvider.identify()` and
return the same `RecognitionResult` shape, so handlers/workers never need to
know which provider answered. See ARCHITECTURE.md §6 for which provider is
live-tested vs. implemented-per-spec.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class StreamingLink:
    platform: str  # "spotify" | "apple_music" | "deezer" | "youtube"
    url: str


@dataclass(frozen=True)
class RecognitionResult:
    """Normalized result of a music identification attempt.

    `matched=False` means the provider call succeeded but found no confident
    match — this is a normal, expected outcome (most short/noisy/hummed clips
    won't match), never treated as an error.
    """

    matched: bool
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    release_date: str | None = None
    cover_art_url: str | None = None
    score: int | None = None  # 0-100 confidence, when the provider reports one
    links: tuple[StreamingLink, ...] = field(default_factory=tuple)
    raw_provider: str = ""

    @property
    def is_low_confidence(self) -> bool:
        """True when we matched something but the provider signaled low confidence.

        ACRCloud reports a 0-100 score; AudD does not report a score at all for
        its standard endpoint (a match is either returned or it isn't), so this
        is only meaningful for providers that populate `score`.
        """
        return self.matched and self.score is not None and self.score < 70


class RecognitionProviderError(Exception):
    """Raised for genuine provider/transport failures (bad API key, network
    timeout, malformed response, rate limit) — NOT for a normal no-match, which
    is represented by `RecognitionResult(matched=False)` instead.
    """


class MusicRecognitionProvider(Protocol):
    name: str

    async def identify(self, audio_path: Path) -> RecognitionResult:
        """Identify the song in a short local audio file.

        Raises RecognitionProviderError on transport/auth/provider failures.
        Returns a RecognitionResult(matched=False) on a clean no-match.
        """
        ...
