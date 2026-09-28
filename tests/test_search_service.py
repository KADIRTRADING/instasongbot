"""Unit tests for the music-search service layer, the auto-quality picker,
the settings store, and the search/result Redis caches.

Network is mocked at the httpx boundary for the iTunes client (a real live
call is covered by the opt-in tests/test_search_live.py). Everything else is
pure logic or a real fakeredis round trip.
"""

from __future__ import annotations

import json

import fakeredis.aioredis
import httpx
import pytest

from app.constants import MediaType
from app.services.downloader.models import MediaFormat, ProbeResult
from app.services.downloader.quality import pick_auto_format
from app.services.search.base import SearchProviderError
from app.services.search.itunes_client import ITunesSearchProvider, _detect_version, _upsize_artwork

# --- iTunes client: version detection + artwork upsize (pure) ----------------


@pytest.mark.parametrize(
    "track,collection,expected",
    [
        ("Levitating", "Future Nostalgia", ""),
        ("Levitating (Slowed + Reverb)", "", "slowed"),
        ("Levitating (Blessed Madonna Remix)", "", "remix"),
        ("Levitating (Live)", "Live Album", "live"),
        ("Levitating (Acoustic)", "", "acoustic"),
        ("Levitating (Karaoke Version)", "", "cover"),
    ],
)
def test_detect_version(track, collection, expected) -> None:
    assert _detect_version(track, collection) == expected


def test_upsize_artwork_swaps_size_segment() -> None:
    url = "https://is1.mzstatic.com/image/thumb/x/100x100bb.jpg"
    assert _upsize_artwork(url) == "https://is1.mzstatic.com/image/thumb/x/600x600bb.jpg"


def test_upsize_artwork_none_and_nonmatching() -> None:
    assert _upsize_artwork(None) is None
    assert _upsize_artwork("https://x/cover.png") == "https://x/cover.png"


# --- iTunes client: parsing + ranking with a mocked HTTP transport -----------


def _itunes_payload(results):
    return json.dumps({"resultCount": len(results), "results": results}).encode()


def _mock_transport(payload: bytes, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


async def test_itunes_search_parses_and_ranks(monkeypatch) -> None:
    results = [
        {"trackName": "Levitating (feat. DaBaby)", "artistName": "Dua Lipa", "collectionName": "FN",
         "trackTimeMillis": 203000, "previewUrl": "https://p/1.m4a", "trackViewUrl": "https://o/1",
         "artworkUrl100": "https://a/100x100bb.jpg"},
        {"trackName": "Levitating", "artistName": "Dua Lipa", "collectionName": "FN",
         "trackTimeMillis": 203000, "previewUrl": "https://p/2.m4a", "trackViewUrl": "https://o/2",
         "artworkUrl100": "https://a/100x100bb.jpg"},
        {"trackName": "Levitating (Remix)", "artistName": "Dua Lipa", "collectionName": "FN",
         "trackTimeMillis": 203000, "previewUrl": None, "trackViewUrl": "https://o/3",
         "artworkUrl100": "https://a/100x100bb.jpg"},
    ]
    provider = ITunesSearchProvider()

    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(_itunes_payload(results))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    parsed = await provider.search("levitating", limit=10)

    # Exact match "Levitating" ranked first (over the feat. variant), remix last.
    assert parsed[0].title == "Levitating"
    assert parsed[0].duration_seconds == 203
    assert parsed[0].is_downloadable_preview is True
    assert parsed[0].artwork_url.endswith("600x600bb.jpg")
    # The remix (a variant, and no preview) is not downloadable-preview.
    remix = next(r for r in parsed if r.version == "remix")
    assert remix.is_downloadable_preview is False
    assert remix.preview_url is None


async def test_itunes_search_empty_query_returns_empty() -> None:
    assert await ITunesSearchProvider().search("   ", limit=10) == []


async def test_itunes_search_http_error_raises_provider_error(monkeypatch) -> None:
    provider = ITunesSearchProvider()
    real_client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        kwargs["transport"] = _mock_transport(b"nope", status=503)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)

    with pytest.raises(SearchProviderError):
        await provider.search("x", limit=10)


# --- quality picker ----------------------------------------------------------


def _mf(fid, mt, w=None, h=None):
    return MediaFormat(format_id=fid, media_type=mt, label=fid, ext="mp4", width=w, height=h)


def _probe(formats):
    return ProbeResult(
        platform="youtube", source_url="u", title="t", uploader=None,
        thumbnail_url=None, duration_seconds=1, formats=tuple(formats),
    )


def test_pick_auto_format_best_and_caps() -> None:
    formats = [
        _mf("v1080", MediaType.VIDEO, 1920, 1080),
        _mf("v720", MediaType.VIDEO, 1280, 720),
        _mf("v480", MediaType.VIDEO, 854, 480),
        _mf("a", MediaType.AUDIO),
    ]
    pr = _probe(formats)
    assert pick_auto_format(pr, "best").format_id == "v1080"
    assert pick_auto_format(pr, "720").format_id == "v720"
    assert pick_auto_format(pr, "480").format_id == "v480"
    assert pick_auto_format(pr, "audio").format_id == "a"
    assert pick_auto_format(pr, "bogus").format_id == "v1080"  # invalid -> best


def test_pick_auto_format_audio_falls_back_to_video_when_no_audio() -> None:
    pr = _probe([_mf("best", MediaType.VIDEO, 720, 1280)])
    assert pick_auto_format(pr, "audio").format_id == "best"


def test_pick_auto_format_cap_all_above_picks_smallest() -> None:
    pr = _probe([_mf("v1080", MediaType.VIDEO, 1920, 1080), _mf("v720", MediaType.VIDEO, 1280, 720)])
    assert pick_auto_format(pr, "480").format_id == "v720"


def test_pick_auto_format_empty_returns_none() -> None:
    assert pick_auto_format(_probe([]), "best") is None


# --- settings_store ----------------------------------------------------------


@pytest.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import app.db.models  # noqa: F401 - ensure all tables (incl. bot_settings) are registered on Base
    from app.db.base import Base

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield sm
    await engine.dispose()


async def test_settings_store_quality_defaults_and_override(db) -> None:
    from app.bot.settings_store import get_auto_video_quality, set_auto_video_quality

    async with db() as s:
        # Falls back to env default when unset.
        assert await get_auto_video_quality(s, env_default="720") == "720"
        # Invalid env default -> "best".
        assert await get_auto_video_quality(s, env_default="garbage") == "best"
        # Admin override wins.
        await set_auto_video_quality(s, "480")
        await s.commit()
    async with db() as s:
        assert await get_auto_video_quality(s, env_default="best") == "480"


async def test_settings_store_quality_rejects_invalid() -> None:
    from app.bot.settings_store import set_auto_video_quality

    class _Dummy:
        pass

    with pytest.raises(ValueError):
        await set_auto_video_quality(_Dummy(), "nonsense")  # validated before touching the session


async def test_settings_store_show_result_buttons_default_and_toggle(db) -> None:
    from app.bot.settings_store import get_show_result_buttons, set_show_result_buttons

    async with db() as s:
        assert await get_show_result_buttons(s) is True  # default on
        await set_show_result_buttons(s, False)
        await s.commit()
    async with db() as s:
        assert await get_show_result_buttons(s) is False


# --- caches (search_cache + result_cache) round-trip -------------------------


@pytest.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


async def test_search_cache_roundtrip_paging_and_user_binding(redis) -> None:
    from app.bot.search_cache import (
        SearchSession,
        load_search_session,
        new_token,
        store_search_session,
    )
    from app.services.search.base import SearchResult

    results = tuple(
        SearchResult(
            title=f"t{i}", artist="a", album="al", duration_seconds=200,
            preview_url="p", official_url="o", artwork_url="art", is_downloadable_preview=True,
        )
        for i in range(23)
    )
    sess = SearchSession(user_id=42, query="q", results=results, per_page=10)
    token = new_token()
    await store_search_session(redis, token, sess)

    loaded = await load_search_session(redis, token)
    assert loaded is not None
    assert loaded.total_pages == 3
    assert loaded.belongs_to(42) and not loaded.belongs_to(7)
    assert len(loaded.page(0)) == 10 and len(loaded.page(2)) == 3
    assert loaded.result_at(5).title == "t5"
    assert loaded.result_at(99) is None
    assert await load_search_session(redis, "missing") is None


async def test_result_cache_roundtrip(redis) -> None:
    from app.bot.result_cache import ResultActionContext, load_result_context, store_result_context

    ctx = ResultActionContext(user_id=42, file_id="F", source_url="https://x", platform="tiktok", probe_job_id="pj")
    await store_result_context(redis, "tok", ctx)
    loaded = await load_result_context(redis, "tok")
    assert loaded == ctx and loaded.belongs_to(42)
    assert await load_result_context(redis, "missing") is None
