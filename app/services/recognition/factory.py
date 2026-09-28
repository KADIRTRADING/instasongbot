"""Selects the configured recognition provider. One switch, not a code fork."""

from __future__ import annotations

from app.config import Settings
from app.constants import RecognitionProviderName
from app.services.recognition.acrcloud_provider import ACRCloudProvider
from app.services.recognition.audd_provider import AudDProvider
from app.services.recognition.base import MusicRecognitionProvider


def get_recognition_provider(settings: Settings) -> MusicRecognitionProvider:
    if settings.RECOGNITION_PROVIDER == RecognitionProviderName.AUDD:
        return AudDProvider(api_token=settings.AUDD_API_TOKEN, timeout_seconds=settings.RECOGNITION_TIMEOUT_SECONDS)
    if settings.RECOGNITION_PROVIDER == RecognitionProviderName.ACRCLOUD:
        return ACRCloudProvider(
            host=settings.ACRCLOUD_HOST,
            access_key=settings.ACRCLOUD_ACCESS_KEY,
            access_secret=settings.ACRCLOUD_ACCESS_SECRET,
            timeout_seconds=settings.RECOGNITION_TIMEOUT_SECONDS,
        )
    raise ValueError(f"Unknown RECOGNITION_PROVIDER: {settings.RECOGNITION_PROVIDER!r}")
