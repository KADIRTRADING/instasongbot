"""AudD music recognition provider.

API docs: https://docs.audd.io/
Auth: a single `api_token` form field, no request signing.
Endpoint: POST https://api.audd.io/ with the audio as multipart `file`.

Response shapes actually observed in development (see ARCHITECTURE.md §6):
  - Match:    {"status": "success", "result": {"artist": ..., "title": ..., ...}}
  - No match: {"status": "success", "result": null}
  - Error:    {"status": "error", "error": {"error_code": int, "error_message": str}}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.services.recognition.base import (
    MusicRecognitionProvider,
    RecognitionProviderError,
    RecognitionResult,
    StreamingLink,
)

AUDD_ENDPOINT = "https://api.audd.io/"


class AudDProvider(MusicRecognitionProvider):
    name = "audd"

    def __init__(self, api_token: str, timeout_seconds: int = 20) -> None:
        if not api_token:
            raise ValueError("AUDD_API_TOKEN is required when RECOGNITION_PROVIDER=audd")
        self._api_token = api_token
        self._timeout = timeout_seconds

    async def identify(self, audio_path: Path) -> RecognitionResult:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                with audio_path.open("rb") as fh:
                    response = await client.post(
                        AUDD_ENDPOINT,
                        data={
                            "api_token": self._api_token,
                            "return": "apple_music,spotify,deezer",
                        },
                        files={"file": (audio_path.name, fh, "application/octet-stream")},
                    )
        except httpx.TimeoutException as exc:
            raise RecognitionProviderError(f"AudD request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RecognitionProviderError(f"AudD request failed: {exc}") from exc

        if response.status_code != 200:
            raise RecognitionProviderError(f"AudD returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RecognitionProviderError(f"AudD returned non-JSON response: {response.text[:300]}") from exc

        return self._parse(payload)

    @staticmethod
    def _parse(payload: dict[str, Any]) -> RecognitionResult:
        status = payload.get("status")

        if status == "error":
            error = payload.get("error") or {}
            code = error.get("error_code")
            message = error.get("error_message", "unknown error")
            raise RecognitionProviderError(f"AudD error {code}: {message}")

        if status != "success":
            raise RecognitionProviderError(f"AudD returned unexpected status: {status!r}")

        result = payload.get("result")
        if not result:
            return RecognitionResult(matched=False, raw_provider="audd")

        links: list[StreamingLink] = []
        song_link = result.get("song_link")
        if song_link:
            links.append(StreamingLink(platform="lis.tn", url=song_link))

        spotify = result.get("spotify") or {}
        spotify_url = (spotify.get("external_urls") or {}).get("spotify")
        if spotify_url:
            links.append(StreamingLink(platform="spotify", url=spotify_url))

        apple_music = result.get("apple_music") or {}
        apple_url = apple_music.get("url")
        if apple_url:
            links.append(StreamingLink(platform="apple_music", url=apple_url))

        deezer = result.get("deezer") or {}
        deezer_url = deezer.get("link")
        if deezer_url:
            links.append(StreamingLink(platform="deezer", url=deezer_url))

        cover_art_url = None
        apple_artwork = apple_music.get("artwork") or {}
        if apple_artwork.get("url"):
            # Apple's artwork URL is a template like ".../{w}x{h}bb.jpg" — fill in a sane size.
            cover_art_url = apple_artwork["url"].replace("{w}", "500").replace("{h}", "500")
        elif deezer.get("album", {}).get("cover_big"):
            cover_art_url = deezer["album"]["cover_big"]

        return RecognitionResult(
            matched=True,
            title=result.get("title"),
            artist=result.get("artist"),
            album=result.get("album"),
            release_date=result.get("release_date"),
            cover_art_url=cover_art_url,
            score=None,  # AudD's standard endpoint doesn't report a confidence score.
            links=tuple(links),
            raw_provider="audd",
        )
