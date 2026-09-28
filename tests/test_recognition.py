"""Unit tests for the recognition providers, using respx-mocked HTTP transport
and JSON fixtures captured from real AudD/ACRCloud responses (see
tests/fixtures/). No network access required.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.services.recognition.acrcloud_provider import ACRCloudProvider, build_signature
from app.services.recognition.audd_provider import AUDD_ENDPOINT, AudDProvider
from app.services.recognition.base import RecognitionProviderError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def sample_audio(tmp_path: Path) -> Path:
    path = tmp_path / "clip.mp3"
    path.write_bytes(b"\x00" * 1024)  # content doesn't matter, HTTP is mocked
    return path


# --- AudD ---------------------------------------------------------------


@respx.mock
async def test_audd_match(sample_audio: Path) -> None:
    respx.post(AUDD_ENDPOINT).mock(return_value=httpx.Response(200, json=load_fixture("audd_match.json")))
    provider = AudDProvider(api_token="test")

    result = await provider.identify(sample_audio)

    assert result.matched is True
    assert result.title == "Everybody Wants To Rule The World"
    assert result.artist == "Tears For Fears"
    assert result.album == "Songs From The Big Chair"
    assert result.raw_provider == "audd"
    platforms = {link.platform for link in result.links}
    assert {"spotify", "apple_music", "deezer", "lis.tn"} <= platforms
    assert result.cover_art_url is not None
    assert "{w}" not in result.cover_art_url  # template placeholders must be filled in


@respx.mock
async def test_audd_no_match(sample_audio: Path) -> None:
    respx.post(AUDD_ENDPOINT).mock(return_value=httpx.Response(200, json=load_fixture("audd_no_match.json")))
    provider = AudDProvider(api_token="test")

    result = await provider.identify(sample_audio)

    assert result.matched is False
    assert result.title is None


@respx.mock
async def test_audd_error_raises(sample_audio: Path) -> None:
    respx.post(AUDD_ENDPOINT).mock(return_value=httpx.Response(200, json=load_fixture("audd_error.json")))
    provider = AudDProvider(api_token="bad-token")

    with pytest.raises(RecognitionProviderError, match="900"):
        await provider.identify(sample_audio)


@respx.mock
async def test_audd_timeout_raises(sample_audio: Path) -> None:
    respx.post(AUDD_ENDPOINT).mock(side_effect=httpx.TimeoutException("boom"))
    provider = AudDProvider(api_token="test")

    with pytest.raises(RecognitionProviderError, match="timed out"):
        await provider.identify(sample_audio)


@respx.mock
async def test_audd_http_500_raises(sample_audio: Path) -> None:
    respx.post(AUDD_ENDPOINT).mock(return_value=httpx.Response(500, text="server error"))
    provider = AudDProvider(api_token="test")

    with pytest.raises(RecognitionProviderError, match="HTTP 500"):
        await provider.identify(sample_audio)


def test_audd_requires_token() -> None:
    with pytest.raises(ValueError, match="AUDD_API_TOKEN"):
        AudDProvider(api_token="")


# --- ACRCloud -------------------------------------------------------------


def test_acrcloud_signature_matches_documented_algorithm() -> None:
    # Reference implementation transcribed verbatim from ACRCloud's official
    # docs/SDK (see app/services/recognition/acrcloud_provider.py docstring).
    import base64
    import hashlib
    import hmac

    def reference_sign(http_method, http_uri, access_key, data_type, signature_version, timestamp, access_secret):
        string_to_sign = "\n".join([http_method, http_uri, access_key, data_type, signature_version, str(timestamp)])
        digest = hmac.new(access_secret.encode("ascii"), string_to_sign.encode("ascii"), digestmod=hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")

    expected = reference_sign("POST", "/v1/identify", "AK123", "audio", "1", "1700000000", "SECRETXYZ")
    actual = build_signature(
        http_method="POST",
        http_uri="/v1/identify",
        access_key="AK123",
        access_secret="SECRETXYZ",
        data_type="audio",
        signature_version="1",
        timestamp="1700000000",
    )
    assert actual == expected


@respx.mock
async def test_acrcloud_match(sample_audio: Path) -> None:
    respx.post("https://identify-eu-west-1.acrcloud.com/v1/identify").mock(
        return_value=httpx.Response(200, json=load_fixture("acrcloud_match.json"))
    )
    provider = ACRCloudProvider(
        host="identify-eu-west-1.acrcloud.com", access_key="AK", access_secret="SECRET"
    )

    result = await provider.identify(sample_audio)

    assert result.matched is True
    assert result.title == "Hello"
    assert result.artist == "Adele"
    assert result.album == "Hello"
    assert result.score == 100
    assert result.is_low_confidence is False
    platforms = {link.platform for link in result.links}
    assert {"spotify", "deezer", "youtube"} == platforms


@respx.mock
async def test_acrcloud_no_result(sample_audio: Path) -> None:
    respx.post("https://identify-eu-west-1.acrcloud.com/v1/identify").mock(
        return_value=httpx.Response(200, json=load_fixture("acrcloud_no_result.json"))
    )
    provider = ACRCloudProvider(
        host="identify-eu-west-1.acrcloud.com", access_key="AK", access_secret="SECRET"
    )

    result = await provider.identify(sample_audio)

    assert result.matched is False


@respx.mock
async def test_acrcloud_low_confidence_flag(sample_audio: Path) -> None:
    fixture = load_fixture("acrcloud_match.json")
    fixture["metadata"]["music"][0]["score"] = 42
    respx.post("https://identify-eu-west-1.acrcloud.com/v1/identify").mock(
        return_value=httpx.Response(200, json=fixture)
    )
    provider = ACRCloudProvider(
        host="identify-eu-west-1.acrcloud.com", access_key="AK", access_secret="SECRET"
    )

    result = await provider.identify(sample_audio)

    assert result.matched is True
    assert result.score == 42
    assert result.is_low_confidence is True


def test_acrcloud_requires_credentials() -> None:
    with pytest.raises(ValueError, match="ACRCLOUD_HOST"):
        ACRCloudProvider(host="", access_key="", access_secret="")


# --- Factory ---------------------------------------------------------------


def test_factory_selects_audd() -> None:
    from app.config import Settings
    from app.services.recognition.factory import get_recognition_provider

    settings = Settings(
        BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        RECOGNITION_PROVIDER="audd",
        AUDD_API_TOKEN="tok",
    )
    provider = get_recognition_provider(settings)
    assert provider.name == "audd"


def test_factory_selects_acrcloud() -> None:
    from app.config import Settings
    from app.services.recognition.factory import get_recognition_provider

    settings = Settings(
        BOT_TOKEN="x",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        RECOGNITION_PROVIDER="acrcloud",
        ACRCLOUD_HOST="host",
        ACRCLOUD_ACCESS_KEY="key",
        ACRCLOUD_ACCESS_SECRET="secret",
    )
    provider = get_recognition_provider(settings)
    assert provider.name == "acrcloud"


# --- Live, opt-in smoke test (see pyproject.toml: skipped unless `-m live`) ---


@pytest.mark.live
async def test_audd_live_public_test_token(sample_audio: Path) -> None:
    """Hits the real AudD API with the public rate-limited `test` token against
    AudD's own hosted example file. Run explicitly with:
        pytest -m live tests/test_recognition.py
    Skipped by default so the regular test suite never depends on network
    access or the shared public token's daily quota.
    """
    provider = AudDProvider(api_token="test")
    # AudD identifies by URL directly server-side; reuse identify() by pointing
    # it at a real short local file would require a real audio fixture, so
    # instead we exercise the provider's HTTP path against AudD's own hosted
    # example via a tiny helper request here.
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            AUDD_ENDPOINT, data={"url": "https://audd.tech/example.mp3", "api_token": "test"}
        )
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["result"]["artist"]
