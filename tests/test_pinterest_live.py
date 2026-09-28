"""Opt-in live tests that hit the real Pinterest public resource API.

Skipped by default (see pyproject.toml `addopts = -m "not live"`). Run with:
    pytest -m live tests/test_pinterest_live.py
"""

from __future__ import annotations

import pytest

from app.constants import MediaType
from app.services.downloader.pinterest_client import PinterestClient

pytestmark = pytest.mark.live


async def test_live_probe_known_public_video_pin() -> None:
    client = PinterestClient()
    result = await client.probe("https://www.pinterest.com/pin/664281013778109217/")
    assert result.formats
    assert any(f.media_type == MediaType.VIDEO for f in result.formats)


async def test_live_probe_known_public_carousel_pin() -> None:
    client = PinterestClient()
    result = await client.probe("https://www.pinterest.com/pin/631207704071379020/")
    assert len(result.formats) >= 2
    assert all(f.media_type == MediaType.IMAGE for f in result.formats)
