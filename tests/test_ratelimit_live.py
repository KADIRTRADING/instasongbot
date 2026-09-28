"""Opt-in live test proving the rate limiter's core safety property — that
concurrent requests cannot race past the configured limit — against a REAL
Redis server, not a fake. This is the guarantee that matters most for abuse
prevention, so it's worth verifying against real server-side Lua execution
rather than trusting fakeredis's in-process emulation alone.

Requires a Redis instance reachable at redis://127.0.0.1:6379/0 (see the
docker-compose-based dev Redis used throughout this project's development).
Skipped by default. Run with: pytest -m live tests/test_ratelimit_live.py
"""

from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.services.ratelimit.limiter import RateLimiter

pytestmark = pytest.mark.live


@pytest.fixture
async def real_redis():
    client = Redis.from_url("redis://127.0.0.1:6379/0")
    yield client
    await client.aclose()


async def test_live_concurrent_requests_never_exceed_limit(real_redis: Redis) -> None:
    limiter = RateLimiter(real_redis)
    await limiter.reset(user_id=777, action="stress_test")  # clean slate

    results = await asyncio.gather(
        *[limiter.check(user_id=777, action="stress_test", limit=5, window_seconds=10) for _ in range(50)]
    )

    allowed_count = sum(1 for r in results if r.allowed)
    assert allowed_count == 5, f"Expected exactly 5 allowed requests under real concurrency, got {allowed_count}"


async def test_live_basic_allow_deny_cycle(real_redis: Redis) -> None:
    limiter = RateLimiter(real_redis)
    await limiter.reset(user_id=778, action="basic_test")

    d1 = await limiter.check(user_id=778, action="basic_test", limit=2, window_seconds=10)
    d2 = await limiter.check(user_id=778, action="basic_test", limit=2, window_seconds=10)
    d3 = await limiter.check(user_id=778, action="basic_test", limit=2, window_seconds=10)

    assert d1.allowed and d2.allowed
    assert not d3.allowed
    assert d3.retry_after_seconds > 0
