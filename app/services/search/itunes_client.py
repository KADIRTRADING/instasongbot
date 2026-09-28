"""Apple iTunes Search API provider (song name / artist -> results).

API docs: https://performance-partners.apple.com/search-api
Endpoint:  GET https://itunes.apple.com/search
Auth:      none (free, keyless, no signing) — chosen for exactly that reason.

Fields we rely on (all live-verified against the real API during development):
  - artistName, trackName, collectionName, trackTimeMillis
  - trackViewUrl      -> official Apple Music/iTunes page for the full song
  - previewUrl        -> a REAL, fetchable ~30s M4A clip (audio/x-m4p), which
                         Apple provides expressly for preview use. We send it
                         to the user CLEARLY LABELED as a 30-second preview
                         (see app/i18n search_preview_* keys), never as the
                         full track. `is_downloadable_preview` is True only
                         when previewUrl is present.
  - artworkUrl100     -> 100x100 art; we upsize the size segment for a nicer
                         cover (Apple serves the larger size from the same URL
                         pattern).

We ask the API for `entity=song` so results are individual tracks, not albums
or music videos, which keeps the numbered list meaningful.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.services.search.base import (
    MusicSearchProvider,
    SearchProviderError,
    SearchResult,
)

ITUNES_ENDPOINT = "https://itunes.apple.com/search"

# Ordered longest/most-specific first so "slowed + reverb" tags as "slowed"
# and a plain "remix" still matches. Each maps a detected substring (in the
# track OR collection name, case-insensitive) to the short version tag stored
# on SearchResult.version. Kept small and unambiguous on purpose.
_VERSION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("slowed", re.compile(r"\bslowed\b|\bsped up\b|\bnightcore\b", re.IGNORECASE)),
    ("remix", re.compile(r"\bremix\b|\bedit\b|\bbootleg\b|\bflip\b", re.IGNORECASE)),
    ("live", re.compile(r"\blive\b|\blive at\b|\bin concert\b|\bunplugged\b", re.IGNORECASE)),
    ("acoustic", re.compile(r"\bacoustic\b", re.IGNORECASE)),
    ("cover", re.compile(r"\bcover\b|\btribute\b|\bkaraoke\b|\bmade famous by\b", re.IGNORECASE)),
)


def _detect_version(track_name: str, collection_name: str) -> str:
    haystack = f"{track_name} {collection_name}"
    for tag, pattern in _VERSION_PATTERNS:
        if pattern.search(haystack):
            return tag
    return ""


def _upsize_artwork(url100: str | None) -> str | None:
    """Apple's artworkUrl100 ends in `.../100x100bb.jpg`; the same URL serves
    larger art if we swap that size segment. 600x600 is a good Telegram cover
    size. Any URL not matching the expected pattern is returned unchanged.
    """
    if not url100:
        return None
    return re.sub(r"/\d+x\d+bb\.", "/600x600bb.", url100)


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace for exact-match ranking comparison."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class ITunesSearchProvider(MusicSearchProvider):
    name = "itunes"

    def __init__(self, *, country: str = "US", timeout_seconds: int = 15) -> None:
        self._country = country or "US"
        self._timeout = timeout_seconds

    async def search(self, query: str, *, limit: int) -> list[SearchResult]:
        query = (query or "").strip()
        if not query:
            return []

        params = {
            "term": query,
            "media": "music",
            "entity": "song",
            "limit": str(max(1, min(limit, 200))),
            "country": self._country,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(ITUNES_ENDPOINT, params=params)
        except httpx.TimeoutException as exc:
            raise SearchProviderError(f"iTunes search timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise SearchProviderError(f"iTunes search failed: {exc}") from exc

        if response.status_code != 200:
            raise SearchProviderError(f"iTunes returned HTTP {response.status_code}: {response.text[:200]}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise SearchProviderError(f"iTunes returned non-JSON response: {response.text[:200]}") from exc

        raw_results = payload.get("results") or []
        results = [self._parse_one(item) for item in raw_results if item.get("trackName")]
        return self._rank(results, query)

    @staticmethod
    def _parse_one(item: dict[str, Any]) -> SearchResult:
        track_name = item.get("trackName") or ""
        collection_name = item.get("collectionName") or ""
        preview_url = item.get("previewUrl")
        millis = item.get("trackTimeMillis")
        duration_seconds = int(millis // 1000) if isinstance(millis, (int, float)) and millis else None

        return SearchResult(
            title=track_name,
            artist=item.get("artistName") or "",
            album=collection_name or None,
            duration_seconds=duration_seconds,
            preview_url=preview_url or None,
            official_url=item.get("trackViewUrl") or None,
            artwork_url=_upsize_artwork(item.get("artworkUrl100")),
            is_downloadable_preview=bool(preview_url),
            version=_detect_version(track_name, collection_name),
        )

    @staticmethod
    def _rank(results: list[SearchResult], query: str) -> list[SearchResult]:
        """Rank exact matches first (the brief requires this), then originals
        ahead of variants (remix/live/...), preserving the API's own relative
        order within each tier (Python's sort is stable). "Exact" means the
        query equals the track title, "artist — title", or "title — artist",
        after normalization.
        """
        q = _normalize(query)

        def sort_key(r: SearchResult) -> tuple[int, int]:
            title = _normalize(r.title)
            artist = _normalize(r.artist)
            combined_at = f"{artist} {title}"
            combined_ta = f"{title} {artist}"
            if q == title or q == combined_at or q == combined_ta:
                exactness = 0
            elif q in title or (artist and artist in q and title in q):
                exactness = 1
            else:
                exactness = 2
            # Originals (no version tag) before variants within the same tier.
            is_variant = 1 if r.version else 0
            return (exactness, is_variant)

        return sorted(results, key=sort_key)
