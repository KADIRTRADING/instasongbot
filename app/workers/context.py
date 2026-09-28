"""Shared resources built once when the worker process starts, then handed to
every job via arq's `ctx` dict. See app/workers/settings.py's on_startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.services.downloader.manager import DownloadManager
from app.services.media.ffmpeg_tools import MediaTools
from app.services.recognition.base import MusicRecognitionProvider
from app.services.search.base import MusicSearchProvider
from app.services.storage.base import StorageBackend


@dataclass
class WorkerContext:
    settings: Settings
    bot: Bot
    sessionmaker: async_sessionmaker
    redis: Redis
    recognition_provider: MusicRecognitionProvider
    download_manager: DownloadManager
    media_tools: MediaTools
    storage_backend: StorageBackend
    search_provider: MusicSearchProvider
