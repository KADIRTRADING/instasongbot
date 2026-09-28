"""ACRCloud music recognition provider.

Implements ACRCloud's published Identification API protocol directly over HTTP
(HMAC-SHA1 signed multipart/form-data to POST https://{host}/v1/identify) rather
than depending on ACRCloud's binary SDK (`acrcloud_extr_tool`), which ships as a
platform-specific compiled extension and would complicate the Docker image for
no real benefit — signing a request is ~15 lines of stdlib `hmac`/`hashlib`.

Docs: https://docs.acrcloud.com/reference/identification-api/identification-api

IMPORTANT (see ARCHITECTURE.md §6): this implementation is written and unit-
tested against ACRCloud's documented protocol and recorded response fixtures,
but it has NOT been exercised against a real ACRCloud project in this project
(that requires a registered ACRCloud account's own host/access_key/access_secret,
which we don't have). Verify against your own ACRCloud console before depending
on it in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

import httpx

from app.services.recognition.base import (
    MusicRecognitionProvider,
    RecognitionProviderError,
    RecognitionResult,
    StreamingLink,
)

IDENTIFY_URI = "/v1/identify"

# ACRCloud status codes that mean "the request worked, but nothing matched" —
# see https://docs.acrcloud.com/docs/acrcloud/metadata/status-code/
NO_RESULT_CODE = 1001


def build_signature(
    *, http_method: str, http_uri: str, access_key: str, access_secret: str, data_type: str, signature_version: str, timestamp: str
) -> str:
    """Reproduce ACRCloud's documented `string_to_sign` + HMAC-SHA1 exactly."""
    string_to_sign = "\n".join([http_method, http_uri, access_key, data_type, signature_version, timestamp])
    digest = hmac.new(access_secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


class ACRCloudProvider(MusicRecognitionProvider):
    name = "acrcloud"

    def __init__(self, host: str, access_key: str, access_secret: str, timeout_seconds: int = 20) -> None:
        if not (host and access_key and access_secret):
            raise ValueError(
                "ACRCLOUD_HOST, ACRCLOUD_ACCESS_KEY and ACRCLOUD_ACCESS_SECRET are all required "
                "when RECOGNITION_PROVIDER=acrcloud"
            )
        self._host = host
        self._access_key = access_key
        self._access_secret = access_secret
        self._timeout = timeout_seconds

    async def identify(self, audio_path: Path) -> RecognitionResult:
        audio_bytes = audio_path.read_bytes()
        timestamp = str(int(time.time()))
        signature = build_signature(
            http_method="POST",
            http_uri=IDENTIFY_URI,
            access_key=self._access_key,
            access_secret=self._access_secret,
            data_type="audio",
            signature_version="1",
            timestamp=timestamp,
        )

        data = {
            "access_key": self._access_key,
            "sample_bytes": str(len(audio_bytes)),
            "timestamp": timestamp,
            "signature": signature,
            "data_type": "audio",
            "signature_version": "1",
        }
        files = {"sample": (audio_path.name, audio_bytes, "application/octet-stream")}
        url = f"https://{self._host}{IDENTIFY_URI}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, data=data, files=files)
        except httpx.TimeoutException as exc:
            raise RecognitionProviderError(f"ACRCloud request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RecognitionProviderError(f"ACRCloud request failed: {exc}") from exc

        if response.status_code != 200:
            raise RecognitionProviderError(f"ACRCloud returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RecognitionProviderError(f"ACRCloud returned non-JSON response: {response.text[:300]}") from exc

        return self._parse(payload)

    @staticmethod
    def _parse(payload: dict[str, Any]) -> RecognitionResult:
        status = payload.get("status") or {}
        code = status.get("code")

        if code == 0:
            music_list = ((payload.get("metadata") or {}).get("music")) or []
            if not music_list:
                return RecognitionResult(matched=False, raw_provider="acrcloud")
            return ACRCloudProvider._parse_music_entry(music_list[0])

        if code == NO_RESULT_CODE:
            return RecognitionResult(matched=False, raw_provider="acrcloud")

        # Any other code is a genuine failure (bad signature, quota exceeded, etc).
        raise RecognitionProviderError(f"ACRCloud error {code}: {status.get('msg', 'unknown error')}")

    @staticmethod
    def _parse_music_entry(entry: dict[str, Any]) -> RecognitionResult:
        artists = entry.get("artists") or []
        artist_name = ", ".join(a["name"] for a in artists if a.get("name")) or None

        links: list[StreamingLink] = []
        external = entry.get("external_metadata") or {}
        spotify_id = (external.get("spotify") or {}).get("track", {}).get("id")
        if spotify_id:
            links.append(StreamingLink(platform="spotify", url=f"https://open.spotify.com/track/{spotify_id}"))
        youtube_vid = (external.get("youtube") or {}).get("vid")
        if youtube_vid:
            links.append(StreamingLink(platform="youtube", url=f"https://www.youtube.com/watch?v={youtube_vid}"))
        deezer_id = (external.get("deezer") or {}).get("track", {}).get("id")
        if deezer_id:
            links.append(StreamingLink(platform="deezer", url=f"https://www.deezer.com/track/{deezer_id}"))

        return RecognitionResult(
            matched=True,
            title=entry.get("title"),
            artist=artist_name,
            album=(entry.get("album") or {}).get("name"),
            release_date=entry.get("release_date"),
            cover_art_url=None,  # ACRCloud's Identify API does not return cover art directly.
            score=entry.get("score"),
            links=tuple(links),
            raw_provider="acrcloud",
        )
