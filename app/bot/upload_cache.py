"""Short-lived Redis cache for an uploaded video's Telegram `file_id`.

Mirrors app/bot/probe_cache.py's rationale exactly, but for the "user
uploaded a video file" path instead of "user sent a link": a real Telegram
file_id is 80+ bytes, comfortably over the 64-byte callback_data ceiling (see
app/bot/callback_data.py), so `VideoActionCallback` can only ever carry a
short reference key — the actual file_id is stashed here and looked up when
the action callback fires.
"""

from __future__ import annotations

from redis.asyncio import Redis

_KEY_PREFIX = "upload_file_id"
_DEFAULT_TTL_SECONDS = 30 * 60  # matches probe_cache's TTL — plenty of time to tap a button


def _key(upload_job_id: str) -> str:
    return f"{_KEY_PREFIX}:{upload_job_id}"


async def store_upload_file_id(redis: Redis, upload_job_id: str, file_id: str, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    await redis.set(_key(upload_job_id), file_id, ex=ttl_seconds)


async def load_upload_file_id(redis: Redis, upload_job_id: str) -> str | None:
    raw = await redis.get(_key(upload_job_id))
    if raw is None:
        return None
    return raw.decode() if isinstance(raw, bytes) else raw
