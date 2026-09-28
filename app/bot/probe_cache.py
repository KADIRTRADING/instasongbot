"""Short-lived Redis cache for `ProbeResult` objects.

Why this exists: probing a link (app/services/downloader/manager.py's
`probe()`) and downloading it happen in two separate steps so the user can
choose a format first. The probe runs in a background job (`probe_job`) and
the download is triggered later by an inline-keyboard callback — those are
two different arq invocations, potentially on two different worker processes,
so the `ProbeResult` has to be handed off through something shared, not kept
in-process. Redis with a short TTL is the natural fit (same Redis already
used for the job queue and rate limiter).

Keyed by the *probe job's* id, not the download job's id, since one probe can
back multiple downloads (e.g. a user who taps "720p" and then goes back and
also grabs "audio only" from the same link).
"""

from __future__ import annotations

import json
from dataclasses import asdict

from redis.asyncio import Redis

from app.services.downloader.models import MediaFormat, ProbeResult

_KEY_PREFIX = "probe_result"
_DEFAULT_TTL_SECONDS = 30 * 60  # long enough to browse a multi-image carousel and pick several


def _key(probe_job_id: str) -> str:
    return f"{_KEY_PREFIX}:{probe_job_id}"


async def store_probe_result(redis: Redis, probe_job_id: str, probe: ProbeResult, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
    payload = {
        "platform": probe.platform,
        "source_url": probe.source_url,
        "title": probe.title,
        "uploader": probe.uploader,
        "thumbnail_url": probe.thumbnail_url,
        "duration_seconds": probe.duration_seconds,
        "formats": [
            {**asdict(fmt), "media_type": fmt.media_type.value} for fmt in probe.formats
        ],
        "backend_payload": probe.backend_payload,
    }
    await redis.set(_key(probe_job_id), json.dumps(payload), ex=ttl_seconds)


async def load_probe_result(redis: Redis, probe_job_id: str) -> ProbeResult | None:
    raw = await redis.get(_key(probe_job_id))
    if raw is None:
        return None

    from app.constants import MediaType

    data = json.loads(raw)
    formats = tuple(
        MediaFormat(
            format_id=f["format_id"],
            media_type=MediaType(f["media_type"]),
            label=f["label"],
            ext=f["ext"],
            filesize_bytes=f.get("filesize_bytes"),
            width=f.get("width"),
            height=f.get("height"),
        )
        for f in data["formats"]
    )
    return ProbeResult(
        platform=data["platform"],
        source_url=data["source_url"],
        title=data["title"],
        uploader=data["uploader"],
        thumbnail_url=data["thumbnail_url"],
        duration_seconds=data["duration_seconds"],
        formats=formats,
        backend_payload=data["backend_payload"],
    )
