"""Redis-backed sliding-window rate limiter.

Uses the classic "sliding window log" pattern: a per-(action, user) Redis
sorted set where each member is one request timestamp. A single Lua script
does trim-expired + count + (conditionally) add atomically in one round trip,
so concurrent requests from the same user can't race past the limit (which a
naive check-then-add in two separate round trips would allow).

Limits themselves are NOT hard-coded here — `check()` takes `limit` and
`window_seconds` as arguments, resolved by the caller (a middleware) from
`bot_settings` (hot, admin-editable) with an env-var fallback (cold default).
This keeps the limiter itself a pure mechanism, reusable for the three
per-action limits (recognize/download/convert) and the separate global burst
limit described in ARCHITECTURE.md §10.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS[1] = the sorted-set key for this (action, user_id)
# ARGV[1] = now, in milliseconds (integer)
# ARGV[2] = window size, in milliseconds (integer)
# ARGV[3] = limit (integer, max requests allowed per window)
# ARGV[4] = a unique member for this request (timestamp + random suffix, so
#           two requests in the same millisecond never collide as one member)
#
# Returns {allowed(0/1), remaining, retry_after_ms}. All Lua numbers come back
# as Redis integers (Lua->RESP conversion truncates floats), which is why the
# whole script works in integer milliseconds rather than float seconds.
_SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now_ms - window_ms)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now_ms, member)
    redis.call('PEXPIRE', key, window_ms + 1000)
    return {1, limit - count - 1, 0}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after_ms = window_ms
    if oldest[2] then
        retry_after_ms = tonumber(oldest[2]) + window_ms - now_ms
    end
    if retry_after_ms < 0 then
        retry_after_ms = 0
    end
    return {0, 0, retry_after_ms}
end
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class RateLimiter:
    def __init__(self, redis: Redis, key_prefix: str = "ratelimit") -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._script = redis.register_script(_SLIDING_WINDOW_SCRIPT)

    def _key(self, action: str, user_id: int) -> str:
        return f"{self._prefix}:{action}:{user_id}"

    async def check(self, *, user_id: int, action: str, limit: int, window_seconds: int) -> RateLimitDecision:
        """Atomically record one attempt and report whether it's within the
        limit for the last `window_seconds`. Always records the attempt in
        the sorted set when allowed — callers should treat a denied result as
        "do not proceed", since the script only adds a member on the allowed
        branch (a denied check does not itself count against future windows).
        """
        now_ms = int(time.time() * 1000)
        window_ms = window_seconds * 1000
        member = f"{now_ms}-{uuid.uuid4().hex}"

        allowed, remaining, retry_after_ms = await self._script(
            keys=[self._key(action, user_id)], args=[now_ms, window_ms, limit, member]
        )
        return RateLimitDecision(
            allowed=bool(allowed), remaining=int(remaining), retry_after_seconds=retry_after_ms / 1000
        )

    async def reset(self, *, user_id: int, action: str) -> None:
        """Clear a user's window for one action. Mainly useful for tests and
        for an admin "unban/reset limits" action.
        """
        await self._redis.delete(self._key(action, user_id))
