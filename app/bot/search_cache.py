"""Short-lived Redis cache for a music-search session's results + page state.

Mirrors app/bot/probe_cache.py's rationale: a text search runs in a background
job (`search_job`), but the numbered-list navigation (Next/Previous/pick a
number) happens later via inline-keyboard callbacks, potentially handled by a
different process. The full ordered result list therefore has to live in
something shared, keyed by a short token that fits in callback_data.

User-binding: the stored payload records the `user_id` that requested the
search, and `SearchSession.belongs_to()` lets the callback handler reject a
tap from anyone else (the brief requires callbacks be bound to the requesting
user). The token is a short uuid4 hex slice — unguessable enough for this, and
short enough to leave room in the 64-byte callback_data budget (see
app/bot/callback_data.py) alongside a prefix and a page/index number.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from uuid import uuid4

from redis.asyncio import Redis

from app.services.search.base import SearchResult

_KEY_PREFIX = "search_session"
_DEFAULT_TTL_SECONDS = 15 * 60  # plenty of time to page through and pick a track


def new_token() -> str:
    """A short, URL/callback-safe token for one search session."""
    return uuid4().hex[:12]


@dataclass(frozen=True)
class SearchSession:
    user_id: int
    query: str
    results: tuple[SearchResult, ...]
    per_page: int

    def belongs_to(self, user_id: int) -> bool:
        return self.user_id == user_id

    @property
    def total_pages(self) -> int:
        if not self.results:
            return 1
        return (len(self.results) + self.per_page - 1) // self.per_page

    def page(self, page_index: int) -> list[SearchResult]:
        """Zero-based page of results (clamped to valid range)."""
        page_index = max(0, min(page_index, self.total_pages - 1))
        start = page_index * self.per_page
        return list(self.results[start : start + self.per_page])

    def result_at(self, absolute_index: int) -> SearchResult | None:
        if 0 <= absolute_index < len(self.results):
            return self.results[absolute_index]
        return None


def _key(token: str) -> str:
    return f"{_KEY_PREFIX}:{token}"


async def store_search_session(
    redis: Redis,
    token: str,
    session: SearchSession,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> None:
    payload = {
        "user_id": session.user_id,
        "query": session.query,
        "per_page": session.per_page,
        "results": [asdict(r) for r in session.results],
    }
    await redis.set(_key(token), json.dumps(payload), ex=ttl_seconds)


async def load_search_session(redis: Redis, token: str) -> SearchSession | None:
    raw = await redis.get(_key(token))
    if raw is None:
        return None
    data = json.loads(raw)
    results = tuple(SearchResult(**r) for r in data["results"])
    return SearchSession(
        user_id=data["user_id"],
        query=data["query"],
        results=results,
        per_page=data["per_page"],
    )
