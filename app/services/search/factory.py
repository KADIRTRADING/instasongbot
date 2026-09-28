"""Selects the configured music-search provider. One switch, not a code fork
(mirrors app/services/recognition/factory.py).
"""

from __future__ import annotations

from app.config import Settings
from app.services.search.base import MusicSearchProvider
from app.services.search.itunes_client import ITunesSearchProvider


def get_search_provider(settings: Settings) -> MusicSearchProvider:
    provider = (settings.SEARCH_PROVIDER or "itunes").lower()
    if provider == "itunes":
        return ITunesSearchProvider(
            country=settings.SEARCH_COUNTRY,
            timeout_seconds=settings.SEARCH_TIMEOUT_SECONDS,
        )
    raise ValueError(f"Unknown SEARCH_PROVIDER: {settings.SEARCH_PROVIDER!r}")
