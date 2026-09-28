"""Short-lived Redis cache tying an auto-downloaded video's inline "result
action" buttons (Find this song / Extract MP3 / Other options) back to the
media WITHOUT refetching it.

Why this exists: in the automatic flow, a social link is downloaded and the
video is sent to the user in one shot (see app/workers/tasks.py's
`auto_download_job`). We then offer optional follow-up actions under it. When
the user taps "Find this song", we must NOT download the reel again (the brief
is explicit about reusing the already-obtained media). The most reliable reuse
handle is the Telegram `file_id` of the video we just uploaded — Telegram
hosts it, and recognize_job/convert_job already accept a `source_file_id`. So
after a successful auto-download+send, the worker stashes that file_id (plus
the source URL / probe token, so "Other quality/options" can re-probe for a
different format) here, keyed by a short token carried in the ResultAction
callback.

`user_id` is recorded so a tap can be rejected if it doesn't come from the
user the media was sent to (callbacks bound to the requesting user).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

_KEY_PREFIX = "result_action"
_DEFAULT_TTL_SECONDS = 30 * 60  # matches probe/upload cache TTLs


@dataclass(frozen=True)
class ResultActionContext:
    user_id: int
    # The Telegram file_id of the video we just delivered — reused for
    # recognize/convert so we never refetch the source. Empty when the
    # delivered media was too large for direct upload (link-only delivery),
    # in which case only re-probe-based actions ("Other options") remain.
    file_id: str
    source_url: str
    platform: str
    # The probe_job_id whose cached ProbeResult still describes this URL's
    # available formats, so "Other quality/options" can present the full
    # format keyboard without a fresh probe. May be "" if unavailable.
    probe_job_id: str

    def belongs_to(self, user_id: int) -> bool:
        return self.user_id == user_id


def _key(token: str) -> str:
    return f"{_KEY_PREFIX}:{token}"


async def store_result_context(
    redis: Redis, token: str, context: ResultActionContext, ttl_seconds: int = _DEFAULT_TTL_SECONDS
) -> None:
    await redis.set(_key(token), json.dumps(asdict(context)), ex=ttl_seconds)


async def load_result_context(redis: Redis, token: str) -> ResultActionContext | None:
    raw = await redis.get(_key(token))
    if raw is None:
        return None
    return ResultActionContext(**json.loads(raw))
