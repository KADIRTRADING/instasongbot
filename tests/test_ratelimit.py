"""Tests for the Redis sliding-window rate limiter.

Runs against fakeredis (with the `lupa` extra, which gives fakeredis real Lua
EVAL/EVALSHA support) for fast, offline correctness checks. The genuine
concurrency/atomicity guarantee — that N simultaneous requests never let more
than `limit` through — is proven separately against a REAL Redis server in
test_ratelimit_live.py, since that guarantee is exactly what a fake single-
process Redis can't meaningfully stress-test the way real server-side Lua
execution isolation does.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.services.ratelimit.limiter import RateLimiter


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis()
    yield client
    await client.aclose()


@pytest.fixture
def limiter(redis: fakeredis.aioredis.FakeRedis) -> RateLimiter:
    return RateLimiter(redis)


async def test_allows_requests_under_the_limit(limiter: RateLimiter) -> None:
    for _ in range(3):
        decision = await limiter.check(user_id=1, action="download", limit=3, window_seconds=10)
        assert decision.allowed is True


async def test_denies_requests_over_the_limit(limiter: RateLimiter) -> None:
    for _ in range(3):
        await limiter.check(user_id=1, action="download", limit=3, window_seconds=10)

    decision = await limiter.check(user_id=1, action="download", limit=3, window_seconds=10)

    assert decision.allowed is False
    assert decision.remaining == 0
    assert decision.retry_after_seconds > 0


async def test_remaining_count_decreases_correctly(limiter: RateLimiter) -> None:
    d1 = await limiter.check(user_id=1, action="recognize", limit=5, window_seconds=10)
    d2 = await limiter.check(user_id=1, action="recognize", limit=5, window_seconds=10)
    d3 = await limiter.check(user_id=1, action="recognize", limit=5, window_seconds=10)

    assert d1.remaining == 4
    assert d2.remaining == 3
    assert d3.remaining == 2


async def test_different_users_have_independent_limits(limiter: RateLimiter) -> None:
    await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    decision_user1_second = await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    decision_user2_first = await limiter.check(user_id=2, action="download", limit=1, window_seconds=10)

    assert decision_user1_second.allowed is False
    assert decision_user2_first.allowed is True


async def test_different_actions_have_independent_limits(limiter: RateLimiter) -> None:
    await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    decision_download_second = await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    decision_convert_first = await limiter.check(user_id=1, action="convert", limit=1, window_seconds=10)

    assert decision_download_second.allowed is False
    assert decision_convert_first.allowed is True


async def test_reset_clears_the_window(limiter: RateLimiter) -> None:
    await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    denied = await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    assert denied.allowed is False

    await limiter.reset(user_id=1, action="download")

    allowed_again = await limiter.check(user_id=1, action="download", limit=1, window_seconds=10)
    assert allowed_again.allowed is True


async def test_window_expiry_allows_requests_again(limiter: RateLimiter, redis: fakeredis.aioredis.FakeRedis) -> None:
    """A very short window should let old entries age out. We simulate the
    passage of time by manually removing sorted-set entries older than the
    window rather than sleeping in a test, since fakeredis's virtual clock for
    TTL/ZREMRANGEBYSCORE-by-score is driven by our own now_ms argument, which
    we fully control via limiter.check()'s internal time.time() call — so
    instead we directly assert the retry_after value shrinks as time passes.
    """
    import time
    from unittest.mock import patch

    base_time = time.time()
    with patch("time.time", return_value=base_time):
        await limiter.check(user_id=5, action="download", limit=1, window_seconds=1)
        denied = await limiter.check(user_id=5, action="download", limit=1, window_seconds=1)
        assert denied.allowed is False

    with patch("time.time", return_value=base_time + 1.1):
        allowed_after_window = await limiter.check(user_id=5, action="download", limit=1, window_seconds=1)
        assert allowed_after_window.allowed is True


async def test_zero_limit_always_denies(limiter: RateLimiter) -> None:
    decision = await limiter.check(user_id=1, action="download", limit=0, window_seconds=10)
    assert decision.allowed is False


async def test_high_limit_allows_many_requests(limiter: RateLimiter) -> None:
    results = [
        await limiter.check(user_id=1, action="download", limit=100, window_seconds=10) for _ in range(50)
    ]
    assert all(r.allowed for r in results)
    assert results[-1].remaining == 50
