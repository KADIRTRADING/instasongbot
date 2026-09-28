"""Tests for the Pinterest client, using JSON fixtures captured from real,
live Pinterest pins during development (single image, single video, and a
real 4-image carousel post — see tests/fixtures/pinterest_*.json). No network
access required to run these.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.constants import MediaType
from app.services.downloader.errors import ContentNotFoundError, PrivateContentError
from app.services.downloader.models import MediaFormat, ProbeResult
from app.services.downloader.pinterest_client import PinterestClient

FIXTURES = Path(__file__).parent / "fixtures"
RESOURCE_URL = "https://www.pinterest.com/resource/PinResource/get/"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.mark.asyncio
@respx.mock
async def test_probe_single_image_pin() -> None:
    respx.get(RESOURCE_URL).mock(return_value=httpx.Response(200, json=load_fixture("pinterest_image.json")))
    client = PinterestClient()

    result = await client.probe("https://www.pinterest.com/pin/3729612238075167/")

    assert result.platform == "pinterest"
    assert len(result.formats) == 1
    assert result.formats[0].media_type == MediaType.IMAGE
    assert result.formats[0].format_id == "image:0"


@pytest.mark.asyncio
@respx.mock
async def test_probe_single_video_pin() -> None:
    respx.get(RESOURCE_URL).mock(return_value=httpx.Response(200, json=load_fixture("pinterest_video.json")))
    client = PinterestClient()

    result = await client.probe("https://www.pinterest.com/pin/664281013778109217/")

    assert len(result.formats) == 1
    fmt = result.formats[0]
    assert fmt.media_type == MediaType.VIDEO
    # V_720P is a progressive MP4 and should be preferred over the HLS variants.
    assert fmt.format_id == "video:V_720P"
    assert result.duration_seconds is not None
    assert result.duration_seconds > 0


@pytest.mark.asyncio
@respx.mock
async def test_probe_carousel_pin_returns_one_format_per_image() -> None:
    fixture = load_fixture("pinterest_carousel.json")
    expected_count = len(fixture["resource_response"]["data"]["carousel_data"]["carousel_slots"])
    respx.get(RESOURCE_URL).mock(return_value=httpx.Response(200, json=fixture))
    client = PinterestClient()

    result = await client.probe("https://www.pinterest.com/pin/631207704071379020/")

    assert len(result.formats) == expected_count == 4
    assert all(f.media_type == MediaType.IMAGE for f in result.formats)
    assert [f.format_id for f in result.formats] == [f"carousel:{i}" for i in range(expected_count)]
    assert result.formats[0].label == "Image 1 of 4"


@pytest.mark.asyncio
@respx.mock
async def test_pin_not_found_raises() -> None:
    respx.get(RESOURCE_URL).mock(return_value=httpx.Response(404))
    client = PinterestClient()

    with pytest.raises(ContentNotFoundError):
        await client.probe("https://www.pinterest.com/pin/999999999999999999/")


@pytest.mark.asyncio
@respx.mock
async def test_private_pin_raises_private_content_error() -> None:
    respx.get(RESOURCE_URL).mock(
        return_value=httpx.Response(
            200,
            json={"resource_response": {"status": "failure", "code": 401, "message": "This pin is private"}},
        )
    )
    client = PinterestClient()

    with pytest.raises(PrivateContentError):
        await client.probe("https://www.pinterest.com/pin/123456/")


def test_resolve_format_url_for_image() -> None:
    fixture = load_fixture("pinterest_image.json")
    data = fixture["resource_response"]["data"]
    probe = ProbeResult(
        platform="pinterest",
        source_url="https://www.pinterest.com/pin/x/",
        title=None,
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=(MediaFormat(format_id="image:0", media_type=MediaType.IMAGE, label="Image", ext="jpg"),),
        backend_payload={"pin_data": data},
    )
    url, fmt = PinterestClient.resolve_format_url(probe, "image:0")
    assert url == data["images"]["orig"]["url"]
    assert fmt.format_id == "image:0"


def test_resolve_format_url_for_carousel_rewrites_to_originals() -> None:
    fixture = load_fixture("pinterest_carousel.json")
    data = fixture["resource_response"]["data"]

    probe = ProbeResult(
        platform="pinterest",
        source_url="https://www.pinterest.com/pin/x/",
        title=None,
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=tuple(
            MediaFormat(format_id=f"carousel:{i}", media_type=MediaType.IMAGE, label="x", ext="jpg")
            for i in range(len(data["carousel_data"]["carousel_slots"]))
        ),
        backend_payload={"pin_data": data},
    )
    url, _ = PinterestClient.resolve_format_url(probe, "carousel:0")
    assert "/originals/" in url
    assert url.endswith(".jpg")


def test_resolve_format_url_unknown_format_id_raises() -> None:
    probe = ProbeResult(
        platform="pinterest",
        source_url="x",
        title=None,
        uploader=None,
        thumbnail_url=None,
        duration_seconds=None,
        formats=(MediaFormat(format_id="image:0", media_type=MediaType.IMAGE, label="x", ext="jpg"),),
        backend_payload={"pin_data": {}},
    )
    with pytest.raises(ContentNotFoundError):
        PinterestClient.resolve_format_url(probe, "video:V_720P")
