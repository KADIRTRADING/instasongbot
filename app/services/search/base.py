"""Provider-agnostic music text-search interface.

A "search" turns free-text ("dua lipa levitating") into an ordered list of
`SearchResult`s the bot presents as a numbered list. This is DELIBERATELY
distinct from recognition (app/services/recognition): recognition takes an
audio clip and returns at most one confident match; search takes text and
returns many candidates to choose from.

Honesty is a first-class constraint here (see the project brief). A provider
must NEVER claim it can hand the user a full downloadable track it cannot
legitimately redistribute. `SearchResult` therefore separates:
  - `preview_url`      an actually-fetchable short (~30s) audio clip, when the
                       provider offers one for legal preview use, and
  - `official_url`     a link to the full song on an official service.
`is_downloadable_preview` is True only when `preview_url` is set AND the clip
is a real, legally-previewable snippet (not the full track). The bot uses
this to send a clearly-labeled 30-second preview + official link, and falls
back to the official link alone when no preview exists. Nothing in this layer
ever presents a preview as the complete song.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchResult:
    """One candidate track from a text search.

    `version` is a short human tag ("remix", "live", "slowed", ...) detected
    from the track/collection name so the bot can distinguish an original
    from its variants (the brief explicitly asks for this). Empty string for a
    plain original.
    """

    title: str
    artist: str
    album: str | None
    duration_seconds: int | None
    preview_url: str | None
    official_url: str | None
    artwork_url: str | None
    is_downloadable_preview: bool
    version: str = ""  # "" | "remix" | "live" | "slowed" | "cover" | "acoustic"


class SearchProviderError(Exception):
    """Raised for genuine provider/transport failures (network timeout, bad
    HTTP status, malformed response) — NOT for a clean zero-results search,
    which is represented by an empty result list instead.
    """


class MusicSearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        """Return up to `limit` ranked results for `query`.

        Raises SearchProviderError on transport/provider failures. Returns an
        empty list (never raises) on a clean no-results search.
        """
        ...
