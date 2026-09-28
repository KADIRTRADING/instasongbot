"""Opt-in live tests that hit the real Apple iTunes Search API.

Skipped by default (see pyproject.toml `addopts = -m "not live"`). Run with:
    pytest -m live tests/test_search_live.py

The iTunes Search API is free, keyless, and public — these confirm the real
contract this bot depends on (results, a fetchable preview clip, official
links) is still what the code assumes.
"""

from __future__ import annotations

import httpx
import pytest

from app.services.search.itunes_client import ITunesSearchProvider

pytestmark = pytest.mark.live


async def test_live_search_returns_ranked_results_with_preview() -> None:
    provider = ITunesSearchProvider()
    results = await provider.search("dua lipa levitating", limit=10)

    assert results, "expected at least one result for a well-known track"
    # Exact-match ranking puts a plain "Levitating" first.
    assert results[0].title.lower().startswith("levitating")
    # At least one result exposes a real, downloadable preview + official link.
    with_preview = [r for r in results if r.is_downloadable_preview]
    assert with_preview
    assert with_preview[0].official_url and with_preview[0].official_url.startswith("http")


async def test_live_preview_url_is_actually_fetchable() -> None:
    provider = ITunesSearchProvider()
    results = await provider.search("dua lipa levitating", limit=10)
    preview = next(r.preview_url for r in results if r.preview_url)

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        resp = await client.get(preview)
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("audio/")
    assert len(resp.content) > 10_000  # a real ~30s clip, not an empty body


async def test_live_search_no_results_returns_empty_not_error() -> None:
    provider = ITunesSearchProvider()
    results = await provider.search("zzxqwv nonexistent gibberish track 999", limit=10)
    assert isinstance(results, list)  # empty or tiny, but never raises
